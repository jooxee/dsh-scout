#!/usr/bin/env python3
"""Read one Scout's compact lifecycle status without contacting DSH."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any


TERMINAL_PHASES = {
    "turn_completed": "completed",
    "action_required": "action_required",
    "turn_failed": "failed",
}
SESSION_KEY_LIMIT = 200


def default_state_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "state/dsh-scout"


def _command(pid: object, proc_root: Path) -> list[str] | None:
    if type(pid) is not int or pid <= 0:
        return []
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _option_matches(command: list[str], flag: str, expected: str) -> bool:
    return any(command[index : index + 2] == [flag, expected] for index in range(len(command)))


def controller_liveness(state: dict[str, Any], state_path: Path, proc_root: Path) -> str:
    command = _command(state.get("pid"), proc_root)
    if command is None:
        return "unknown"
    if not command:
        return "dead"
    matches = (
        any(Path(part).name == "run_dsh_session.py" for part in command)
        and "--serve" in command
        and _option_matches(command, "--mode", str(state.get("mode", "")))
        and _option_matches(command, "--cwd", str(state.get("cwd", "")))
        and _option_matches(command, "--state-file", str(state_path))
    )
    return "alive" if matches else "mismatch"


def runtime_liveness(state: dict[str, Any], state_path: Path, proc_root: Path) -> str:
    if state.get("backend") != "web":
        return "not_applicable" if state.get("backend") == "sdk" else "unknown"
    web = state.get("web")
    if not isinstance(web, dict):
        return "dead"
    command = _command(web.get("pid"), proc_root)
    if command is None:
        return "unknown"
    if not command:
        return "dead"
    patch = str(state_path.parent / str(state.get("mode", "")) / "web-patch.yml")
    matches = (
        any(Path(part).name == "dsh" for part in command[:3])
        and _option_matches(command, "--patch", patch)
        and _option_matches(command, "--profile", "web")
    )
    return "alive" if matches else "mismatch"


def _safe_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _phase(record: dict[str, Any], kind: str | None, healthy: bool) -> tuple[str, bool]:
    status = record.get("status")
    if status == "running":
        return ("running", True) if healthy else ("stale", False)
    if status == "error" or kind == "turn_failed":
        return "failed", False
    if status == "idle":
        return TERMINAL_PHASES.get(kind, "idle"), False
    if record.get("live_session_lost") or record.get("restarted"):
        return "context_lost", False
    return "unknown", False


def _details(
    state: dict[str, Any], record: dict[str, Any], kind: str | None, now: int
) -> dict[str, Any]:
    started = _safe_int(record.get("prompt_started_at"))
    verification = record.get("verification")
    verification_state = verification.get("state") if isinstance(verification, dict) else None
    backend = state.get("backend")
    return {
        "backend": backend if isinstance(backend, str) and backend in {"web", "sdk"} else None,
        "event_kind": kind if kind in {"turn_started", *TERMINAL_PHASES} else None,
        "started_at": started,
        "elapsed_seconds": (
            max(0, now - started)
            if started is not None and record.get("status") == "running"
            else None
        ),
        "updated_at": _safe_int(record.get("updated_at")),
        "verification": (
            verification_state
            if isinstance(verification_state, str)
            and verification_state in {"pending", "verified"}
            else None
        ),
    }


def snapshot(
    state: dict[str, Any] | None,
    *,
    mode: str,
    session_key: str,
    state_path: Path,
    proc_root: Path = Path("/proc"),
    details: bool = False,
    now: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": 1,
        "mode": mode,
        "session_key": session_key,
        "phase": "missing",
        "active": False,
        "controller": "unknown",
        "runtime": "unknown",
        "turn": None,
        "event_sequence": None,
    }
    if state is None:
        return result
    if state.get("mode") != mode or not isinstance(state.get("sessions"), dict):
        result["phase"] = "unknown"
        return result
    result["controller"] = controller_liveness(state, state_path, proc_root)
    result["runtime"] = runtime_liveness(state, state_path, proc_root)
    record = state["sessions"].get(session_key)
    if not isinstance(record, dict):
        return result

    event = record.get("last_event")
    event = event if isinstance(event, dict) else {}
    kind = event.get("kind") if isinstance(event.get("kind"), str) else None
    result["event_sequence"] = _safe_int(event.get("sequence"))
    result["turn"] = _safe_int(record.get("active_turn")) or _safe_int(record.get("turns"))
    healthy = result["controller"] == "alive" and result["runtime"] in {
        "alive", "not_applicable"
    }
    result["phase"], result["active"] = _phase(record, kind, healthy)

    if details:
        result["details"] = _details(state, record, kind, int(time.time()) if now is None else now)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("write", "read"), required=True)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args()
    if not args.session_key.strip() or len(args.session_key) > SESSION_KEY_LIMIT:
        parser.error(f"--session-key must contain 1..{SESSION_KEY_LIMIT} characters")
    return args


def main() -> int:
    args = parse_args()
    root = (args.state_root or default_state_root()).resolve()
    state_path = root / f"{args.mode}.json"
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError:
        state = None
    except (OSError, ValueError):
        state = {}
    if state is not None and not isinstance(state, dict):
        state = {}
    result = snapshot(
        state,
        mode=args.mode,
        session_key=args.session_key,
        state_path=state_path,
        details=args.details,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

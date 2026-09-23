#!/usr/bin/env python3
"""Persistent DeepSeek Harness SDK session controller.

The public CLI starts one detached daemon per mode (writer or reader), then sends
prompts over a local Unix socket.  Keeping the DSH SDK process alive is required:
the current SDK can create sessions, but cannot reopen a persisted session after
the SDK process exits.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any


DEFAULT_TIMEOUT = 3600


def positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


SOFT_CONTEXT_TOKENS = positive_env_int("DSH_SCOUT_SOFT_CONTEXT_TOKENS", 250_000)
HARD_CONTEXT_TOKENS = positive_env_int("DSH_SCOUT_HARD_CONTEXT_TOKENS", 400_000)
if SOFT_CONTEXT_TOKENS >= HARD_CONTEXT_TOKENS:
    raise ValueError("DSH_SCOUT_SOFT_CONTEXT_TOKENS must be lower than DSH_SCOUT_HARD_CONTEXT_TOKENS")
PROVIDER = os.environ.get("DSH_SCOUT_PROVIDER", "opencode-go")
MODEL = os.environ.get("DSH_SCOUT_MODEL", "glm-5.3-flash")
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def session_id() -> str:
    return f"session-dsh-scout-{uuid.uuid4()}"


def session_record_path(identifier: str) -> Path:
    return DSH_HOME / "storages/session_projcache/sessions" / f"{identifier}.json"


def session_stats(identifier: str) -> dict[str, Any]:
    record = read_json(session_record_path(identifier)).get("record", {})
    rows = record.get("rows", {}) if isinstance(record, dict) else {}
    pressure = rows.get("contextPressure", {}).get("val", {})
    usage = rows.get("tokenUsage", {}).get("val", {})
    totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
    last = usage.get("last", {}) if isinstance(usage, dict) else {}
    turn_boundary = rows.get("turnBoundary", {}).get("val", {})
    return {
        "context_tokens": int(pressure.get("pressureTokens", 0) or 0),
        "context_window": int(pressure.get("contextWindow", 0) or 0),
        "usage_totals": totals if isinstance(totals, dict) else {},
        "last_usage": last if isinstance(last, dict) else {},
        "persisted_turn": int(turn_boundary.get("lastTurn", 0) or 0),
    }


def wait_session_stats(identifier: str, expected_turn: int) -> dict[str, Any]:
    """Wait briefly for the async persistence projection to catch up."""
    latest: dict[str, Any] = {}
    for _ in range(25):
        latest = session_stats(identifier)
        if int(latest.get("persisted_turn", 0)) >= expected_turn:
            return latest
        time.sleep(0.1)
    return latest


class DshSdk:
    def __init__(self, cwd: Path, log_path: Path) -> None:
        self.cwd = cwd
        self.log_path = log_path
        dsh = shutil.which("dsh")
        if dsh is None:
            raise RuntimeError("dsh executable is not available on PATH")
        self.process = subprocess.Popen(
            [dsh, "--profile", "sdk"],
            cwd=cwd,
            env={**os.environ, "DSH_PERMISSION_MODE": "danger-full-access"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self.next_request_id = 1
        self.stderr_thread = threading.Thread(target=self._copy_stderr, daemon=True)
        self.stderr_thread.start()
        request_id = self._send(
            "initialize",
            {"cwd": str(cwd), "provider": PROVIDER, "model": MODEL},
        )
        frame = self._wait_for_response(request_id, 60)
        if "error" in frame:
            raise RuntimeError(f"DSH initialize failed: {frame['error']}")

    def _copy_stderr(self) -> None:
        assert self.process.stderr is not None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as log:
            for line in self.process.stderr:
                log.write(line)
                log.flush()

    def _send(self, method: str, params: dict[str, Any] | None = None) -> int:
        assert self.process.stdin is not None
        request_id = self.next_request_id
        self.next_request_id += 1
        frame: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params
        self.process.stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        return request_id

    def _read_frame(self, deadline: float) -> dict[str, Any]:
        assert self.process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("Timed out waiting for DSH SDK")
            line = self.process.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise RuntimeError(f"DSH SDK exited with code {self.process.poll()}")
        return json.loads(line)

    def _wait_for_response(self, request_id: int, timeout: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            frame = self._read_frame(deadline)
            if frame.get("id") == request_id:
                return frame

    def prompt(self, identifier: str, text: str, timeout: int) -> str:
        request_id = self._send(
            "session/prompt",
            {
                "sessionId": identifier,
                "contentBlocks": [{"type": "text", "text": text}],
            },
        )
        deadline = time.monotonic() + timeout
        accepted = False
        saw_running = False
        latest_text = ""
        failure: str | None = None
        while True:
            frame = self._read_frame(deadline)
            if frame.get("id") == request_id:
                if "error" in frame:
                    raise RuntimeError(f"DSH prompt failed: {frame['error']}")
                accepted = True
                continue
            method = frame.get("method")
            params = frame.get("params", {})
            if params.get("sessionId") != identifier:
                continue
            if method == "session.status":
                status = params.get("status")
                if status == "running":
                    saw_running = True
                elif status == "idle" and accepted and saw_running:
                    if failure:
                        raise RuntimeError(failure)
                    return latest_text.strip()
                continue
            if method != "session.event":
                continue
            event = params.get("event", {})
            event_type = event.get("type")
            data = event.get("data", {})
            if event_type == "assistant/message":
                message = data.get("message", {})
                blocks = message.get("content", []) if isinstance(message, dict) else []
                pieces = [
                    block.get("text", "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if pieces:
                    latest_text = "".join(pieces)
            elif event_type in {"turn/error", "agent/error"}:
                failure = json.dumps(data, ensure_ascii=False)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            request_id = self._send("shutdown")
            self._wait_for_response(request_id, 30)
        except Exception:
            self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def mode_instruction(mode: str) -> str:
    if mode == "write":
        return (
            "MODE: WRITE. You may inspect and modify files in the selected project "
            "within the authorized task scope. Preserve unrelated work and obey the "
            "repository's AGENTS.md, OpenSpec, GitHub Issue, and external-action limits."
        )
    return (
        "MODE: READ_ONLY. Inspect and report only. An outer operating-system sandbox mounts project roots read-only. "
        "Do not attempt mutations or external side effects."
    )


def handoff_prompt() -> str:
    return """Prepare a compact handoff for your successor session. Include only durable information needed to continue this exact task: objective, authoritative Issue/OpenSpec/PR, decisions, files and code state, commands/checks and results, unresolved questions, and the next concrete action. Do not perform new work. Do not include secrets. Return the handoff as Markdown."""


class SessionDaemon:
    def __init__(self, mode: str, cwd: Path, socket_path: Path, state_path: Path) -> None:
        self.mode = mode
        self.cwd = cwd
        self.socket_path = socket_path
        self.state_path = state_path
        self.state_root = state_path.parent
        self.handoff_root = self.state_root / "handoffs"
        self.stopping = False
        previous = read_json(state_path)
        previous_sessions = previous.get("sessions", {})
        self.sessions: dict[str, dict[str, Any]] = {}
        if isinstance(previous_sessions, dict):
            for key, value in previous_sessions.items():
                if isinstance(key, str) and isinstance(value, dict):
                    self.sessions[key] = {
                        "previous_session_id": value.get("session_id"),
                        "restarted": True,
                    }
        self.sdk = DshSdk(cwd, self.state_root / f"{mode}-dsh.log")
        self._save_state()

    def _save_state(self) -> None:
        atomic_json(
            self.state_path,
            {
                "version": 1,
                "pid": os.getpid(),
                "mode": self.mode,
                "cwd": str(self.cwd),
                "socket": str(self.socket_path),
                "provider": PROVIDER,
                "model": MODEL,
                "soft_context_tokens": SOFT_CONTEXT_TOKENS,
                "hard_context_tokens": HARD_CONTEXT_TOKENS,
                "updated_at": int(time.time()),
                "sessions": self.sessions,
            },
        )

    def _session(self, key: str) -> dict[str, Any]:
        current = self.sessions.get(key)
        if current is None or "session_id" not in current:
            current = {
                "session_id": session_id(),
                "turns": 0,
                "context_tokens": 0,
                "context_window": 0,
                "created_at": int(time.time()),
                "restarted": bool(current and current.get("restarted")),
            }
            self.sessions[key] = current
        return current

    def _rotate(self, key: str, current: dict[str, Any], timeout: int) -> tuple[dict[str, Any], str]:
        summary = self.sdk.prompt(current["session_id"], handoff_prompt(), timeout)
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = self.handoff_root / f"{self.mode}-{digest}-{timestamp}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary.rstrip() + "\n")
        replacement = {
            "session_id": session_id(),
            "turns": 0,
            "context_tokens": 0,
            "context_window": 0,
            "created_at": int(time.time()),
            "rotated_from": current["session_id"],
            "handoff_path": str(path),
        }
        self.sessions[key] = replacement
        self._save_state()
        return replacement, summary

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("action") == "ping":
            return {"ok": True, "pid": os.getpid(), "cwd": str(self.cwd), "mode": self.mode}
        if request.get("action") == "shutdown":
            self.stopping = True
            return {"ok": True}
        if request.get("action") != "prompt":
            return {"ok": False, "error": "unknown action"}
        key = request.get("session_key")
        prompt = request.get("prompt")
        timeout = int(request.get("timeout", DEFAULT_TIMEOUT))
        if not isinstance(key, str) or not key.strip():
            return {"ok": False, "error": "session_key must be a non-empty string"}
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        current = self._session(key)
        restarted = bool(current.pop("restarted", False))
        started_at = int(time.time())
        current["status"] = "running"
        current["active_turn"] = int(current.get("turns", 0)) + 1
        current["prompt_started_at"] = started_at
        current["updated_at"] = started_at
        self._save_state()
        rotate = bool(request.get("rotate")) or int(current.get("context_tokens", 0)) >= HARD_CONTEXT_TOKENS
        handoff = ""
        try:
            if rotate and int(current.get("turns", 0)) > 0:
                current, handoff = self._rotate(key, current, timeout)
                current["status"] = "running"
                current["active_turn"] = 1
                current["prompt_started_at"] = started_at
                current["updated_at"] = int(time.time())
                self._save_state()
            full_prompt = f"{mode_instruction(self.mode)}\n\n{prompt}"
            if handoff:
                full_prompt = (
                    f"{mode_instruction(self.mode)}\n\n"
                    "You are continuing a rotated DSH scout session. Treat this handoff as working context; verify mutable facts in the repository.\n\n"
                    f"--- HANDOFF ---\n{handoff}\n--- END HANDOFF ---\n\n{prompt}"
                )
            answer = self.sdk.prompt(current["session_id"], full_prompt, timeout)
        except Exception:
            current["status"] = "error"
            current["last_error_at"] = int(time.time())
            current.pop("active_turn", None)
            self._save_state()
            raise
        expected_turn = int(current.get("turns", 0)) + 1
        stats = wait_session_stats(current["session_id"], expected_turn)
        current.update(stats)
        current["turns"] = expected_turn
        current["status"] = "idle"
        current["last_completed_at"] = int(time.time())
        current.pop("active_turn", None)
        current["updated_at"] = int(time.time())
        self._save_state()
        context_tokens = int(current.get("context_tokens", 0))
        return {
            "ok": True,
            "text": answer,
            "session_key": key,
            "session_id": current["session_id"],
            "turns": current["turns"],
            "context_tokens": context_tokens,
            "context_window": int(current.get("context_window", 0)),
            "soft_limit_reached": context_tokens >= SOFT_CONTEXT_TOKENS,
            "rotated": bool(handoff),
            "restarted": restarted,
            "handoff_path": current.get("handoff_path"),
            "usage_totals": current.get("usage_totals", {}),
        }

    def serve(self) -> int:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(4)
        server.settimeout(1)
        try:
            while not self.stopping:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    payload = b""
                    while not payload.endswith(b"\n"):
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        payload += chunk
                    try:
                        request = json.loads(payload.decode())
                        response = self.handle(request)
                    except Exception as error:
                        response = {"ok": False, "error": str(error)}
                    connection.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        finally:
            server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
            self.sdk.close()
        return 0


def exchange(socket_path: Path, request: dict[str, Any], timeout: int) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path))
        client.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            payload += chunk
    finally:
        client.close()
    value = json.loads(payload.decode())
    if not isinstance(value, dict):
        raise RuntimeError("invalid response from DSH session daemon")
    return value


def daemon_alive(socket_path: Path, cwd: Path, mode: str) -> bool:
    try:
        response = exchange(socket_path, {"action": "ping"}, 2)
    except (OSError, ValueError, RuntimeError):
        return False
    return response.get("ok") is True and response.get("cwd") == str(cwd) and response.get("mode") == mode


def stop_daemon(socket_path: Path) -> None:
    try:
        exchange(socket_path, {"action": "shutdown"}, 5)
    except (OSError, ValueError, RuntimeError):
        pass
    for _ in range(50):
        if not socket_path.exists():
            return
        time.sleep(0.1)


def daemon_command(args: argparse.Namespace, script: Path) -> list[str]:
    base = [
        sys.executable,
        str(script),
        "--serve",
        "--mode",
        args.mode,
        "--cwd",
        str(args.cwd),
        "--socket",
        str(args.socket),
        "--state-file",
        str(args.state_file),
    ]
    if args.mode == "write":
        return base
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("read mode requires bubblewrap (bwrap) on Linux")
    configured_roots = os.environ.get("DSH_SCOUT_READONLY_ROOTS")
    if configured_roots:
        protected_roots = [Path(item).expanduser() for item in configured_roots.split(os.pathsep) if item]
    else:
        home = Path.home()
        protected_roots = [home / "projects", CODEX_HOME / "worktrees", home / "Documents/Codex"]
    mounts: list[str] = []
    seen: set[Path] = set()
    for root in [*protected_roots, args.cwd]:
        root = root.resolve()
        if root in seen:
            continue
        seen.add(root)
        if root.is_dir():
            mounts.extend(["--ro-bind", str(root), str(root)])
    return [
        bwrap,
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        *mounts,
        "--chdir",
        str(args.cwd),
        *base,
    ]


def ensure_daemon(args: argparse.Namespace) -> None:
    if daemon_alive(args.socket, args.cwd, args.mode):
        return
    if args.socket.exists():
        stop_daemon(args.socket)
        try:
            args.socket.unlink()
        except FileNotFoundError:
            pass
    existing = read_json(args.state_file)
    old_pid = existing.get("pid")
    if isinstance(old_pid, int):
        try:
            os.kill(old_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    log_path = args.state_file.parent / f"{args.mode}-controller.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a")
    subprocess.Popen(
        daemon_command(args, Path(__file__).resolve()),
        cwd=args.cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if daemon_alive(args.socket, args.cwd, args.mode):
            return
        time.sleep(0.2)
    raise RuntimeError(f"DSH {args.mode} session daemon did not start; see {log_path}")


def client_main(args: argparse.Namespace) -> int:
    prompt = args.prompt_file.read_text()
    if not prompt.strip():
        raise ValueError("prompt file is empty")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.runtime_dir / f"{args.mode}.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"A DSH {args.mode} request is already running. Allowed topology: one writer plus one reader.",
                file=sys.stderr,
            )
            return 75
        ensure_daemon(args)
        response = exchange(
            args.socket,
            {
                "action": "prompt",
                "session_key": args.session_key,
                "prompt": prompt,
                "timeout": args.timeout_seconds,
                "rotate": args.rotate,
            },
            args.timeout_seconds + 60,
        )
    if not response.get("ok"):
        print(f"DSH request failed: {response.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    if response.get("text"):
        print(response["text"])
    context = int(response.get("context_tokens", 0))
    window = int(response.get("context_window", 0))
    percent = round(context * 100 / window, 1) if window else 0
    print(
        f"DSH {args.mode} session={response.get('session_key')} turn={response.get('turns')} "
        f"context={context}/{window} ({percent}%) rotated={str(bool(response.get('rotated'))).lower()}",
        file=sys.stderr,
    )
    if response.get("restarted"):
        print(
            "DSH controller was restarted; this turn used a fresh live session. The delegation packet must re-establish authoritative context.",
            file=sys.stderr,
        )
    if response.get("soft_limit_reached"):
        print(
            f"DSH context passed the {SOFT_CONTEXT_TOKENS}-token soft limit. Rotate at the next coherent task boundary; hard rotation is automatic at {HARD_CONTEXT_TOKENS}.",
            file=sys.stderr,
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["write", "read"], required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--session-key")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--state-file", type=Path)
    args = parser.parse_args()
    args.cwd = args.cwd.resolve(strict=True)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"codex-dsh-agent-{os.getuid()}"
    state_root = CODEX_HOME / "state/dsh-scout"
    args.runtime_dir = runtime
    args.socket = args.socket or runtime / f"{args.mode}.sock"
    args.state_file = args.state_file or state_root / f"{args.mode}.json"
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if not args.serve:
        if args.prompt_file is None or not args.prompt_file.is_file():
            parser.error("--prompt-file must name an existing file")
        if not args.session_key or not args.session_key.strip():
            parser.error("--session-key is required")
        args.prompt_file = args.prompt_file.resolve(strict=True)
    return args


def main() -> int:
    args = parse_args()
    if args.serve:
        return SessionDaemon(args.mode, args.cwd, args.socket, args.state_file).serve()
    return client_main(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Persistent DeepSeek Harness session controller.

The public CLI starts one detached daemon per mode (writer or reader), then sends
prompts over a local Unix socket. Two execution backends are supported:

* ``web`` (default) — the controller boots one scout-owned ordinary ``dsh web``
  runtime and drives turns over its supported Remote API; this keeps execution
  and UI events in the same owner process so an already-open browser tab on the
  scout runtime shows live tool/assistant streaming and completion.
* ``sdk`` (explicit opt-in) — the controller keeps the legacy DSH SDK process
  alive so a persisted session can be reopened across prompts.

The configured backend is the only one ever used: a startup failure fails the
daemon/turn explicitly and never silently switches to the other backend.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

# Web backend module lives beside this script; make the import work both when
# the daemon runs this file directly and when tests load it by path.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import dsh_web  # noqa: E402


DEFAULT_TIMEOUT = 3600
ACTION_OPEN = "<dsh-scout-action-required-v1>"
ACTION_CLOSE = "</dsh-scout-action-required-v1>"
ACTION_SUMMARY_LIMIT = 500
ACTION_QUESTION_LIMIT = 500
ACTION_QUESTIONS_LIMIT = 3
TERMINAL_EVENT_KINDS = {"action_required", "turn_completed", "turn_failed"}
SUPERVISION_ERROR_LIMIT = 200
SESSION_KEY_LIMIT = 200
SDK_ABORT_TIMEOUT_SECONDS = 5
ACTION_PATTERN = re.compile(
    rf"{re.escape(ACTION_OPEN)}\s*\n(.*?)\n{re.escape(ACTION_CLOSE)}\s*$",
    re.DOTALL,
)

RECOVERY_REASON = "controller-restart-recovery"

VALID_BACKENDS = ("web", "sdk")


def resolve_backend() -> str:
    """Resolve and validate ``DSH_SCOUT_BACKEND`` (default ``web``)."""
    raw = os.environ.get("DSH_SCOUT_BACKEND", "web")
    value = raw.strip()
    if value not in VALID_BACKENDS:
        raise ValueError(
            f"DSH_SCOUT_BACKEND={raw!r} is not supported; choose one of {VALID_BACKENDS}"
        )
    return value


def _client_disconnected(sock: socket.socket | None) -> bool:
    if sock is None:
        return False
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready) and sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (OSError, ValueError):
        return True


class ClientDisconnectedError(ConnectionError):
    """The launcher stopped waiting while its DSH turn was still active."""


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


def parse_action_required(text: str) -> tuple[str, dict[str, Any] | None]:
    """Extract one strictly terminal, bounded scout-declared action request.

    The envelope must terminate the final assistant message. Anything else —
    malformed JSON, unknown fields, an envelope that is not at the end — stays
    ordinary model text and never becomes a supervision event.
    """
    match = ACTION_PATTERN.search(text)
    if match is None:
        return text, None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return text, None
    if not isinstance(value, dict) or set(value) != {"summary", "questions"}:
        return text, None
    summary = value.get("summary")
    questions = value.get("questions")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(questions, list):
        return text, None
    if not questions or any(not isinstance(item, str) or not item.strip() for item in questions):
        return text, None
    bounded = {
        "trust": "scout-declared",
        "summary": summary.strip()[:ACTION_SUMMARY_LIMIT],
        "questions": [
            item.strip()[:ACTION_QUESTION_LIMIT]
            for item in questions[:ACTION_QUESTIONS_LIMIT]
        ],
    }
    return text[: match.start()].rstrip(), bounded


class SupervisionEvents:
    """Append-only, user-private lifecycle events for one controller mode."""

    def __init__(self, path: Path, mode: str) -> None:
        self.path = path
        self.mode = mode
        self.sequence = self._last_sequence()

    def records(self) -> list[dict[str, Any]]:
        """All durably valid JSON event records in the stream, in order."""
        try:
            lines = self.path.read_text().splitlines()
        except (FileNotFoundError, OSError):
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # A truncated or partially written record is ignored until
                # the writer completes it; earlier valid records remain.
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def _last_sequence(self) -> int:
        greatest = 0
        for value in self.records():
            if isinstance(value.get("sequence"), int):
                greatest = max(greatest, value["sequence"])
        return max(0, greatest)

    def recovery_records(self) -> set[tuple[str, str | None, int]]:
        """Controller-derived identities of durable restart recovery failures.

        The identity is ``(session_key, session_id, turn)``: only an exact
        match proves that recovery already happened for that exact abandoned
        turn, so a later fresh DSH session reusing the same key still gets its
        own recovery event.
        """
        records: set[tuple[str, str | None, int]] = set()
        for event in self.records():
            if (
                event.get("kind") == "turn_failed"
                and event.get("reason") == RECOVERY_REASON
                and isinstance(event.get("session_key"), str)
                and isinstance(event.get("sequence"), int)
                and isinstance(event.get("turn"), int)
            ):
                session_id_value = event.get("session_id")
                if session_id_value is not None and not isinstance(session_id_value, str):
                    session_id_value = str(session_id_value)[:SUPERVISION_ERROR_LIMIT]
                records.add(
                    (event["session_key"][:SUPERVISION_ERROR_LIMIT], session_id_value, event["turn"])
                )
        return records

    def append(
        self,
        *,
        kind: str,
        session_key: str,
        session_id_value: str | None,
        turn: int,
        action: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"turn_started", *TERMINAL_EVENT_KINDS}:
            raise ValueError(f"unsupported supervision event kind: {kind}")
        self.sequence += 1
        event: dict[str, Any] = {
            "version": 1,
            "sequence": self.sequence,
            "event_id": f"{self.mode}:{self.sequence}",
            "kind": kind,
            "mode": self.mode,
            "session_key": session_key,
            "session_id": session_id_value,
            "turn": turn,
            "emitted_at": int(time.time()),
        }
        if kind in TERMINAL_EVENT_KINDS:
            event["verification_state"] = "pending"
        if reason is not None:
            event["reason"] = reason[:SUPERVISION_ERROR_LIMIT]
        if action is not None:
            event["action"] = action
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            payload = (json.dumps(event, ensure_ascii=False) + "\n").encode()
            size = os.lseek(descriptor, 0, os.SEEK_END)
            if size > 0:
                # With O_APPEND every write extends EOF; only check the last
                # byte with pread (no offset movement needed).
                if os.pread(descriptor, 1, size - 1) != b"\n":
                    # Finish a partially written record on its own line.
                    os.write(descriptor, b"\n")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        return event


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
            start_new_session=True,
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

    def _read_frame(
        self,
        deadline: float,
        client_socket: socket.socket | None = None,
    ) -> dict[str, Any]:
        assert self.process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ, "sdk")
        if client_socket is not None:
            selector.register(client_socket, selectors.EVENT_READ, "client")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for DSH SDK")
                ready = selector.select(remaining)
                if not ready:
                    raise TimeoutError("Timed out waiting for DSH SDK")
                for key, _ in ready:
                    if key.data != "client":
                        continue
                    try:
                        client_data = client_socket.recv(1, socket.MSG_PEEK) if client_socket is not None else b""
                    except OSError as error:
                        raise ClientDisconnectedError("DSH request client disconnected") from error
                    if client_data == b"":
                        raise ClientDisconnectedError("DSH request client disconnected")
                    raise ConnectionError("DSH request client sent unexpected data")
                if any(key.data == "sdk" for key, _ in ready):
                    line = self.process.stdout.readline()
                    break
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

    def prompt(
        self,
        identifier: str,
        text: str,
        timeout: int,
        client_socket: socket.socket | None = None,
    ) -> str:
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
            frame = self._read_frame(deadline, client_socket)
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
            self.abort()
            return
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.abort()

    def abort(self) -> None:
        """Stop the SDK and every subprocess in its dedicated process group."""
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            self.process.terminate()
        try:
            self.process.wait(timeout=SDK_ABORT_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            self.process.kill()
        self.process.wait(timeout=SDK_ABORT_TIMEOUT_SECONDS)


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


def supervision_instruction() -> str:
    """Signed preamble that makes blocking on a decision a terminal handoff."""
    return (
        "SUPERVISION PROTOCOL. If you are blocked on missing information, missing "
        "authority, or a material product choice you cannot resolve yourself, stop "
        "the turn instead of guessing. End your final message with exactly one "
        "envelope: on its own last line the opening tag, then a JSON object with "
        'exactly the keys "summary" (one short reason, at most 500 characters) and '
        '"questions" (1 to 3 concrete questions, each at most 500 characters), then '
        f"the closing tag:\n{ACTION_OPEN}\n"
        '{"summary":"One short reason","questions":["One concrete question"]}\n'
        f"{ACTION_CLOSE}\n"
        "Rules: this is a terminal handoff, not live dialogue; it is NOT authorization "
        "and does not grant permission. It must appear only at the very end of your "
        "final message. If you can proceed without it, do not emit it. Envelopes that "
        "are malformed, structured in more than one envelope, or not terminal are "
        "treated as ordinary output; if an envelope is valid but its text is longer "
        "than the documented bounds, the controller stores only the clipped prefix."
    )


def handoff_prompt() -> str:
    return """Prepare a compact handoff for your successor session. Include only durable information needed to continue this exact task: objective, authoritative Issue/OpenSpec/PR, decisions, files and code state, commands/checks and results, unresolved questions, and the next concrete action. Do not perform new work. Do not include secrets. Return the handoff as Markdown."""


GIT_FACTS_STATUS_LIMIT = 200
GIT_FACTS_STATUS_LINE_LIMIT = 240
GIT_FACTS_ERROR_LIMIT = 200


def _git(args: list[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    """Run one controller-chosen git command; never anything model-provided."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _validate_prompt(request: dict[str, Any]) -> tuple[tuple[str, str, int] | None, dict[str, Any] | None]:
    """Validate a ``prompt`` action request.

    On success returns ``((key, prompt, timeout), None)``. On failure returns
    ``(None, error_response)``.
    """
    key = request.get("session_key")
    prompt = request.get("prompt")
    try:
        timeout = int(request.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return None, {"ok": False, "error": "timeout must be a positive integer"}
    if timeout <= 0:
        return None, {"ok": False, "error": "timeout must be positive"}
    if not isinstance(key, str) or not key.strip():
        return None, {"ok": False, "error": "session_key must be a non-empty string"}
    if len(key) > SESSION_KEY_LIMIT:
        return None, {"ok": False, "error": f"session_key must be at most {SESSION_KEY_LIMIT} characters"}
    if not isinstance(prompt, str) or not prompt.strip():
        return None, {"ok": False, "error": "prompt must be a non-empty string"}
    return (key, prompt, timeout), None


def _clipped(value: str, limit: int = GIT_FACTS_ERROR_LIMIT) -> str:
    return value.strip()[:limit]


def _clip(value: Any, limit: int = SUPERVISION_ERROR_LIMIT) -> str:
    """Bounded single-line string clip used by failure paths and responses."""
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


def repository_facts(cwd: Path, status_limit: int = GIT_FACTS_STATUS_LIMIT) -> dict[str, Any]:
    """Best-effort, controller-derived Git facts for ``cwd``.

    Returns machine-readable facts only: repository root, HEAD commit, branch,
    porcelain working-tree status (length-clipped), and upstream divergence when
    safely available.  Any failure or non-Git directory yields a bounded error
    record instead of raising, so snapshotting can never fail a prompt dispatch.
    File contents, prompts, secrets, and model output are never captured.
    """
    try:
        probe = _git(["rev-parse", "--show-toplevel"], cwd)
        if probe.returncode != 0:
            # Not a Git worktree (or Git is unavailable); record and move on.
            return {
                "available": False,
                "status": "not-a-git-worktree",
                "error": _clipped(probe.stderr),
            }
        facts: dict[str, Any] = {"available": True}
        facts["repository_root"] = probe.stdout.strip()
        head = _git(["rev-parse", "HEAD"], cwd)
        if head.returncode == 0:
            facts["head"] = head.stdout.strip()
        branch = _git(["branch", "--show-current"], cwd)
        if branch.returncode == 0:
            facts["branch"] = branch.stdout.strip()
        status = _git(["status", "--porcelain=v1"], cwd)
        if status.returncode == 0:
            porcelain = status.stdout.splitlines()
            facts["pending_changes"] = len(porcelain)
            facts["porcelain_status"] = [
                line[:GIT_FACTS_STATUS_LINE_LIMIT] for line in porcelain[:status_limit]
            ]
            facts["status_truncated"] = len(porcelain) > status_limit
            facts["clean"] = not porcelain
        upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
        if upstream.returncode == 0:
            name = upstream.stdout.strip()
            divergence = _git(["rev-list", "--left-right", "--count", f"{name}...HEAD"], cwd)
            parts = divergence.stdout.split()
            if divergence.returncode == 0 and len(parts) == 2:
                facts["upstream"] = {
                    "name": name,
                    "ahead": int(parts[1]),
                    "behind": int(parts[0]),
                }
        return facts
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        return {
            "available": False,
            "status": "git-error",
            "error": _clipped(str(error)),
        }


def recovery_turn(value: Any) -> int:
    """Bounded turn number for a restart recovery event."""
    if isinstance(value, int) and value > 0:
        return value
    return 1


class SessionDaemon:
    # Class-level defaults so ``object.__new__``-constructed test daemons
    # (which bypass ``__init__``) keep a usable attribute surface.
    backend: str = "sdk"
    sdk: Any = None
    web: Any = None
    _backend_lock = threading.RLock()

    @staticmethod
    def _noop_verification_note() -> str:
        """DSH completion alone never verifies repository correctness."""
        return "pending"

    def __init__(
        self,
        mode: str,
        cwd: Path,
        socket_path: Path,
        state_path: Path,
        *,
        backend: str | None = None,
    ) -> None:
        self.mode = mode
        self.cwd = cwd
        self.socket_path = socket_path
        self.state_path = state_path
        self.state_root = state_path.parent
        self.handoff_root = self.state_root / "handoffs"
        self.events = SupervisionEvents(
            self.state_root / "events" / f"{mode}.events.jsonl",
            mode,
        )
        self.stopping = False
        self.backend = backend or resolve_backend()
        if self.backend not in VALID_BACKENDS:
            raise ValueError(
                f"backend {self.backend!r} is not one of {VALID_BACKENDS}"
            )
        previous = read_json(state_path)
        previous_sessions = previous.get("sessions", {})
        self.sessions: dict[str, dict[str, Any]] = {}
        if isinstance(previous_sessions, dict):
            for key, value in previous_sessions.items():
                if isinstance(key, str) and isinstance(value, dict):
                    # Idempotent conversion: the file may already carry
                    # converted restart records from an earlier restart.
                    previous = self._restart_record(value)
                    self.sessions[key] = previous
                    self.sessions[key]["restarted"] = True
        if self.backend == "web":
            dsh_web.reap_stale_runtime(self.state_root, self.mode, os.getuid())
        self._recover_running_sessions()
        self._save_state()
        if self.backend == "sdk":
            self.sdk: DshSdk | None = DshSdk(cwd, self.state_root / f"{mode}-dsh.log")
            self.web: dsh_web.WebBackend | None = None
        else:
            self.sdk = None
            self.web = None
        self._admission_lock = threading.Lock()
        self._backend_lock = threading.RLock()
        self._save_state()

    def _ensure_sdk(self) -> DshSdk:
        if self.backend != "sdk":
            raise RuntimeError("SDK backend is not active")
        if self.sdk is None:
            self.sdk = DshSdk(self.cwd, self.state_root / f"{self.mode}-dsh.log")
        return self.sdk

    def _discard_sdk(self) -> None:
        sdk = self.sdk
        self.sdk = None
        if sdk is None:
            return
        abort = getattr(sdk, "abort", None)
        if callable(abort):
            abort()

    def _ensure_web(self) -> dsh_web.WebBackend:
        with self._backend_lock:
            return self._start_web_if_needed()

    def _reset_dead_web(self) -> None:
        with self._backend_lock:
            if self.web is None:
                return
            process = self.web.runtime.process
            if process is not None and process.poll() is not None:
                self._discard_web()
                for record in self.sessions.values():
                    record["live_session_lost"] = True
                    record["restarted"] = True
                self._save_state()

    def _start_web_if_needed(self) -> dsh_web.WebBackend:
        if self.backend != "web":
            raise RuntimeError("Web backend is not active")
        if self.web is None:
            web_port_env = os.environ.get("DSH_SCOUT_WEB_PORT")
            web_port = int(web_port_env) if web_port_env else None
            backend = dsh_web.WebBackend(
                state_dir=self.state_root,
                mode=self.mode,
                cwd=self.cwd,
                user_home=DSH_HOME,
                provider=PROVIDER,
                model=MODEL,
                host="127.0.0.1",
                port=web_port,
            )
            try:
                backend.start()
            except Exception:
                backend.terminate()
                raise
            self.web = backend
            self._save_state()
        return self.web

    def _discard_web(self) -> None:
        with self._backend_lock:
            if self.web is not None:
                self.web.terminate()
                self.web = None

    def _save_state(self) -> None:
        web_payload: dict[str, Any] = {}
        if self.web is not None and self.web.runtime is not None:
            web_payload = {
                "origin": self.web.runtime.origin,
                "pid": self.web.runtime.process.pid if self.web.runtime.process else None,
                "generation": self.web.generation,
            }
        atomic_json(
            self.state_path,
            {
                "version": 1,
                "pid": os.getpid(),
                "mode": self.mode,
                "backend": self.backend,
                "cwd": str(self.cwd),
                "socket": str(self.socket_path),
                "provider": PROVIDER,
                "model": MODEL,
                "soft_context_tokens": SOFT_CONTEXT_TOKENS,
                "hard_context_tokens": HARD_CONTEXT_TOKENS,
                "updated_at": int(time.time()),
                "web": web_payload,
                "sessions": self.sessions,
            },
        )

    def _restart_record(self, value: dict[str, Any]) -> dict[str, Any]:
        """Convert one persisted session record for restart, idempotently."""
        session_id_value = value.get("session_id")
        if session_id_value is None:
            session_id_value = value.get("previous_session_id")
        status_value = value.get("status")
        if status_value is None:
            status_value = value.get("previous_status")
        turn_value = value.get("previous_active_turn")
        if not isinstance(turn_value, int) or turn_value <= 0:
            turn_value = value.get("active_turn")
        if not isinstance(turn_value, int):
            turn_value = None
        record: dict[str, Any] = {
            "previous_session_id": session_id_value,
            "previous_status": status_value,
            "previous_active_turn": turn_value,
        }
        if value.get("recovered"):
            record["recovered"] = value["recovered"]
        return record

    def _recover_running_sessions(self) -> None:
        """Emit one recovery failure for every session left running last time.

        Deduplication is durable: the event stream itself carries controller-
        derived evidence of an earlier recovery for the same session key, so a
        crash between event append and state persistence cannot duplicate it.
        """
        recovered_records = self.events.recovery_records()
        for key, record in self.sessions.items():
            identity = (
                key[:SUPERVISION_ERROR_LIMIT],
                record.get("previous_session_id"),
                recovery_turn(record.get("previous_active_turn")),
            )
            if identity in recovered_records:
                record["recovered"] = True
                continue
            if not self._needs_recovery(record):
                continue
            self.events.append(
                kind="turn_failed",
                session_key=key,
                session_id_value=record["previous_session_id"],
                turn=recovery_turn(record.get("previous_active_turn")),
                reason=RECOVERY_REASON,
            )
            record["recovered"] = True

    @staticmethod
    def _needs_recovery(record: Any) -> bool:
        return (
            record.get("previous_status") == "running"
            and "previous_session_id" in record
            and not record.get("recovered")
        )

    def _emit_event(
        self,
        current: dict[str, Any],
        kind: str,
        *,
        session_key: str,
        turn: int,
        action: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Append one lifecycle event; persist state FIRST and record the result.

        A failed append is never hidden: the session state records a bounded
        supervision error and the running prompt request fails visibly.
        """
        try:
            event = self.events.append(
                kind=kind,
                session_key=session_key,
                session_id_value=current.get("session_id"),
                turn=turn,
                action=action,
                reason=reason,
            )
        except OSError as error:
            note = f"supervision event append failed: {error.__class__.__name__}"[:SUPERVISION_ERROR_LIMIT]
            current["status"] = "error"
            current["last_error"] = note
            current["last_error_at"] = int(time.time())
            self._save_state()
            raise RuntimeError(f"DSH {self.mode} supervision event append failed: {note}") from error
        current["last_event"] = {
            "event_id": event["event_id"],
            "kind": event["kind"],
            "sequence": event["sequence"],
        }
        self._save_state()
        return event

    def _session(self, key: str) -> dict[str, Any]:
        current = self.sessions.get(key)
        live_session_lost = bool(current and current.get("live_session_lost"))
        if current is None or "session_id" not in current or live_session_lost:
            current = {
                "session_id": session_id(),
                "turns": 0,
                "context_tokens": 0,
                "context_window": 0,
                "created_at": int(time.time()),
                "restarted": bool(current and (current.get("restarted") or live_session_lost)),
            }
            self.sessions[key] = current
        return current

    def _prompt_sdk(
        self,
        identifier: str,
        text: str,
        timeout: int,
        client_socket: socket.socket | None,
    ) -> str:
        sdk = self._ensure_sdk()
        if client_socket is None:
            return sdk.prompt(identifier, text, timeout)
        return sdk.prompt(identifier, text, timeout, client_socket=client_socket)

    def _prompt_web(
        self,
        key: str,
        session_record: dict[str, Any],
        full_prompt: str,
        timeout: int,
        client_socket: socket.socket | None,
    ) -> dict[str, Any]:
        """Dispatch one turn through the scout-owned web runtime.

        Returns a result dict with ``answer``, ``session_id``, ``confirmed_cancel``,
        ``saw_start``, ``turn_end``, ``elapsed``, ``turn``, and ``web_stats``.
        Cancellation success is gated on a confirmed ``turn/end``; otherwise
        the driver has already terminated the owned runtime process group.
        """
        web = self._ensure_web()
        driver = web.make_driver(key)
        identifier = session_record["session_id"]
        driver.ensure_session(session_id=identifier)
        def cancelled() -> bool:
            return self.stopping or _client_disconnected(client_socket)

        try:
            result = driver.run_prompt(
                session_id=identifier, request_id=uuid.uuid4().hex,
                content=[{"type": "text", "text": full_prompt}],
                timeout=float(timeout), cancel_check=cancelled,
            )
        except Exception:
            if cancelled():
                raise ClientDisconnectedError("DSH requester disconnected or controller stopped") from None
            raise
        if result.get("confirmed_cancel"):
            if cancelled():
                raise ClientDisconnectedError("DSH requester disconnected or controller stopped")
            raise TimeoutError("DSH web prompt timed out")
        if result.get("error"):
            raise RuntimeError("DSH provider turn failed: " + _clip(str(result["error"])))
        return {**result, "session_id": identifier}

    def _rotate(
        self,
        key: str,
        current: dict[str, Any],
        timeout: int,
        client_socket: socket.socket | None,
    ) -> tuple[dict[str, Any], str]:
        if self.backend == "sdk":
            summary = self._prompt_sdk(current["session_id"], handoff_prompt(), timeout, client_socket)
        else:
            summary = self._prompt_web(
                key,
                current,
                handoff_prompt(),
                timeout,
                client_socket,
            ).get("answer", "")
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
            "handoff_pre_facts": current.get("handoff_pre_facts", {}),
            "rotated_from": current["session_id"],
            "handoff_path": str(path),
        }
        self.sessions[key] = replacement
        self._save_state()
        return replacement, summary

    def _session_ui_url(self) -> dict[str, Any]:
        """Return the current authenticated UI URL from controller memory.

        A fresh ``--show-ui-url`` call eagerly boots the runtime so the URL
        can be served even before any turn has run.
        """
        if self.backend != "web":
            return {"ok": False, "error": "ui-url is only available on the web backend"}
        try:
            self._reset_dead_web()
            self._ensure_web()
        except dsh_web.WebBackendError as error:
            return {
                "ok": False,
                "error": f"web runtime failed to start: {_clip(str(error))}",
                "backend": self.backend,
            }
        url = self.web.public_url()
        if url is None:
            return {"ok": False, "error": "web runtime is not running yet"}
        return {"ok": True, "url": url, "generation": self.web.generation}

    def handle(
        self,
        request: dict[str, Any],
        client_socket: socket.socket | None = None,
    ) -> dict[str, Any]:
        action = request.get("action")
        if action == "ping":
            return self._handle_ping()
        if action == "shutdown":
            self.stopping = True
            return {"ok": True}
        if action == "ui-url":
            return self._session_ui_url()
        if action == "prompt":
            return self._handle_prompt(request, client_socket)
        return {"ok": False, "error": "unknown action"}

    def _handle_ping(self) -> dict[str, Any]:
        return {
            "ok": True,
            "pid": os.getpid(),
            "cwd": str(self.cwd),
            "mode": self.mode,
            "backend": self.backend,
            "provider": PROVIDER,
            "model": MODEL,
        }

    def _handle_prompt(
        self,
        request: dict[str, Any],
        client_socket: socket.socket | None,
    ) -> dict[str, Any]:
        # Serial admission lock: only one prompt may run at a time per mode.
        # Ping and ui-url do not need the lock.
        if self.stopping or not self._prompt_lock.acquire(blocking=False):
            return {"ok": False, "error": "controller stopping or another prompt is running"}
        try:
            return self._handle_prompt_locked(request, client_socket)
        finally:
            self._prompt_lock.release()

    @property
    def _prompt_lock(self) -> threading.Lock:
        lock = getattr(self, "_admission_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._admission_lock = lock
        return lock

    def _handle_prompt_locked(
        self,
        request: dict[str, Any],
        client_socket: socket.socket | None,
    ) -> dict[str, Any]:
        valid, err = _validate_prompt(request)
        if err is not None:
            return err
        key, prompt, timeout = valid
        self._reset_dead_web()
        current = self._session(key)
        restarted = bool(current.pop("restarted", False))
        for stale in ("previous_session_id", "previous_status", "previous_active_turn", "recovered"):
            current.pop(stale, None)
        return self._run_prompt_after_bootstrap(
            key=key,
            prompt=prompt,
            timeout=timeout,
            request=request,
            client_socket=client_socket,
            current=current,
            restarted=restarted,
        )

    def _run_prompt_after_bootstrap(
        self,
        *,
        key: str,
        prompt: str,
        timeout: int,
        request: dict[str, Any],
        client_socket: socket.socket | None,
        current: dict[str, Any],
        restarted: bool,
    ) -> dict[str, Any]:
        started_at = int(time.time())
        current["status"] = "running"
        current["active_turn"] = int(current.get("turns", 0)) + 1
        current["prompt_started_at"] = started_at
        current["updated_at"] = started_at
        current["handoff_pre_facts"] = repository_facts(self.cwd)
        self._save_state()
        self._emit_event(current, "turn_started", session_key=key, turn=current["active_turn"])

        try:
            handoff = self._maybe_rotate(key, current, timeout, client_socket, request, started_at)
            current = self.sessions[key]
            full_prompt = self._build_full_prompt(key, prompt, handoff)
            dispatch_result = self._dispatch_prompt(
                key=key,
                current=current,
                full_prompt=full_prompt,
                timeout=timeout,
                client_socket=client_socket,
            )
        except ClientDisconnectedError:
            self._record_terminal_failure(current, key, "client-disconnected")
            raise
        except TimeoutError:
            reason = "web-timeout" if self.backend == "web" else "sdk-timeout"
            self._record_terminal_failure(current, key, reason)
            raise
        except dsh_web.CancellationUnconfirmed as error:
            current["last_error"] = _clip(str(error))
            self._record_terminal_failure(current, key, "web-timeout")
            raise RuntimeError(
                f"DSH web turn cancellation was not confirmed: {_clip(str(error))}"
            ) from error
        except Exception as error:
            self._record_terminal_failure(current, key, "prompt-failed", error=_clip(str(error)))
            raise

        return self._finalize_prompt_success(
            key=key,
            current=current,
            answer=dispatch_result["answer"],
            confirmed_cancel=dispatch_result["confirmed_cancel"],
            handoff=handoff,
            restarted=restarted,
        )

    def _maybe_rotate(
        self,
        key: str,
        current: dict[str, Any],
        timeout: int,
        client_socket: socket.socket | None,
        request: dict[str, Any],
        started_at: int,
    ) -> str:
        rotate = bool(request.get("rotate")) or int(current.get("context_tokens", 0)) >= HARD_CONTEXT_TOKENS
        if not rotate or int(current.get("turns", 0)) <= 0:
            return ""
        current, handoff = self._rotate(key, current, timeout, client_socket)
        current["status"] = "running"
        current["active_turn"] = 1
        current["prompt_started_at"] = started_at
        current["updated_at"] = int(time.time())
        self._save_state()
        return handoff

    def _build_full_prompt(self, key: str, prompt: str, handoff: str) -> str:
        header = (
            f"{dsh_web.identity_line(key)}\n\n"
            f"{mode_instruction(self.mode)}\n\n{supervision_instruction()}\n\n"
        )
        if handoff:
            return (
                header
                + "You are continuing a rotated DSH scout session. Treat this handoff as working context; verify mutable facts in the repository.\n\n"
                f"--- HANDOFF ---\n{handoff}\n--- END HANDOFF ---\n\n{prompt}"
            )
        return header + prompt

    def _dispatch_prompt(
        self,
        *,
        key: str,
        current: dict[str, Any],
        full_prompt: str,
        timeout: int,
        client_socket: socket.socket | None,
    ) -> dict[str, Any]:
        if self.backend == "sdk":
            answer = self._prompt_sdk(current["session_id"], full_prompt, timeout, client_socket)
            return {
                "answer": answer,
                "confirmed_cancel": False,
                "session_id": current["session_id"],
            }
        result = self._prompt_web(key, current, full_prompt, timeout, client_socket)
        answer = result.get("answer", "")
        confirmed = bool(result.get("confirmed_cancel"))
        new_session_id = result.get("session_id") or current["session_id"]
        current["session_id"] = new_session_id
        return {
            "answer": answer,
            "confirmed_cancel": confirmed,
            "session_id": new_session_id,
        }

    def _finalize_prompt_success(
        self,
        *,
        key: str,
        current: dict[str, Any],
        answer: str,
        confirmed_cancel: bool,
        handoff: str,
        restarted: bool,
    ) -> dict[str, Any]:
        expected_turn = int(current.get("turns", 0)) + 1
        stats = self._collect_stats(current, expected_turn)
        current.update(stats)
        current["turns"] = expected_turn
        current["status"] = "idle"
        current["last_completed_at"] = int(time.time())
        current.pop("active_turn", None)
        current["updated_at"] = int(time.time())
        current["handoff_post_facts"] = repository_facts(self.cwd)
        current["verification_facts"] = {
            "pre_prompt": current.get("handoff_pre_facts") or {},
            "post_prompt": current.get("handoff_post_facts") or {},
        }
        current["verification"] = {
            "state": "pending",
            "reason": "DSH handoff completed; the orchestrator must independently verify the actual repository state.",
        }
        self._save_state()
        final_text, action = parse_action_required(answer)
        if action is not None:
            kind = "action_required"
            extra = {"action": action}
        else:
            kind = "turn_completed"
            extra = {}
        event = self._emit_event(
            current, kind, session_key=key, turn=expected_turn, **extra
        )
        self._save_state()
        context_tokens = int(current.get("context_tokens", 0))
        ui_url_payload = self._current_ui_url_payload()
        return {
            "ok": True,
            "text": answer if action is None else final_text,
            "session_key": key,
            "session_id": current["session_id"],
            "turns": current["turns"],
            "context_tokens": context_tokens,
            "context_window": int(current.get("context_window", 0)),
            "soft_limit_reached": context_tokens >= SOFT_CONTEXT_TOKENS,
            "rotated": bool(handoff),
            "restarted": restarted,
            "confirmed_cancel": confirmed_cancel,
            "handoff_path": current.get("handoff_path"),
            "state_file": str(self.state_path),
            "verification_state": "pending",
            "verification_facts": current.get("verification_facts"),
            "usage_totals": current.get("usage_totals", {}),
            "event": {
                "event_id": event["event_id"],
                "kind": event["kind"],
                "sequence": event["sequence"],
            },
            "last_event": current["last_event"],
            "action_required": action,
            "backend": self.backend,
            "ui_url": ui_url_payload,
        }

    def _collect_stats(self, current: dict[str, Any], expected_turn: int) -> dict[str, Any]:
        return wait_session_stats(current["session_id"], expected_turn)

    def _current_ui_url_payload(self) -> dict[str, Any] | None:
        if self.backend != "web" or self.web is None:
            return None
        url = self.web.public_url()
        if url is None:
            return None
        return {"url": self.web.origin() + "/", "generation": self.web.generation}

    def _record_terminal_failure(
        self,
        current: dict[str, Any],
        key: str,
        reason: str,
        *,
        error: str | None = None,
    ) -> None:
        """Terminate owned backend, persist state, emit ``turn_failed``.

        The terminal event is published AFTER the owned backend has been
        terminated and the state file has been updated, so a possibly-active
        writer is never kept after a reported failure.
        """
        self._terminate_owned_backend()
        current["status"] = "error"
        current["last_error_at"] = int(time.time())
        if error is not None:
            current["last_error"] = error
        turn_on_failure = int(current.pop("active_turn", 0)) or int(current.get("turns", 0)) + 1
        for record in self.sessions.values():
            record["live_session_lost"] = True
            record["restarted"] = True
        self._save_state()
        self._emit_event(
            current, "turn_failed", session_key=key, turn=turn_on_failure, reason=reason
        )

    def _terminate_owned_backend(self) -> None:
        """Synchronously terminate the owned backend before terminal failure.

        Called on timeout/disconnect/protocol failures so a possibly-active
        writer is never kept after a reported failure. The web turn driver
        already terminates its own runtime via its ``on_unconfirmed``
        callback, so this is a no-op for the web backend on the cancel path;
        we still call ``terminate`` to release file handles and clear the
        backend reference for the next prompt.
        """
        if self.backend == "sdk":
            self._discard_sdk()
        else:
            self._discard_web()

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

        def _install_signal_handlers() -> None:
            def _stop(_signum, _frame) -> None:
                self.stopping = True

            try:
                signal.signal(signal.SIGTERM, _stop)
                signal.signal(signal.SIGINT, _stop)
            except (ValueError, OSError):
                # signal may be unavailable in some environments; the serve
                # loop will still exit on ``stopping`` being set elsewhere.
                pass

        _install_signal_handlers()
        workers: list[threading.Thread] = []
        try:
            while not self.stopping:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    daemon=True,
                )
                workers = [worker for worker in workers if worker.is_alive()]
                workers.append(thread)
                thread.start()
        finally:
            self.stopping = True
            server.close()
            for worker in workers:
                worker.join(timeout=30)
            self._terminate_owned_backend()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
        return 0

    def _serve_connection(self, connection: socket.socket) -> None:
        """One accepted connection: parse, dispatch, reply. Runs on a thread
        so a long-running prompt never blocks other client actions (notably
        ``ui-url``) from being served.
        """
        try:
            connection.settimeout(15)
            payload = b""
            while not payload.endswith(b"\n"):
                chunk = connection.recv(65536)
                if not chunk:
                    break
                payload += chunk
                if len(payload) > (1 << 20):
                    response = {"ok": False, "error": "request too large"}
                    try:
                        connection.sendall(
                            json.dumps(response, ensure_ascii=False).encode() + b"\n"
                        )
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
            try:
                request = json.loads(payload.decode())
            except json.JSONDecodeError:
                response = {"ok": False, "error": "invalid JSON"}
            else:
                try:
                    response = self.handle(request, client_socket=connection)
                except Exception as error:  # noqa: BLE001 - last-resort
                    response = {"ok": False, "error": str(error)}
            try:
                connection.sendall(
                    json.dumps(response, ensure_ascii=False).encode() + b"\n"
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            try:
                connection.close()
            except OSError:
                pass


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


def daemon_alive(
    socket_path: Path,
    cwd: Path,
    mode: str,
    backend: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> bool:
    """Probe the daemon. All identity fields must match.

    A daemon that omits a field it should report (e.g. an SDK daemon running
    before this change did not return ``backend``) is rejected: web will
    never reuse an unknown-backend daemon because that would silently leave
    an SDK writer alive.
    """
    try:
        response = exchange(socket_path, {"action": "ping"}, 2)
    except (OSError, ValueError, RuntimeError):
        return False
    if response.get("ok") is not True:
        return False
    if response.get("cwd") != str(cwd) or response.get("mode") != mode:
        return False
    if response.get("backend") != backend:
        return False
    if provider is not None and response.get("provider") != provider:
        return False
    if model is not None and response.get("model") != model:
        return False
    return True


def stop_daemon(socket_path: Path) -> None:
    try:
        exchange(socket_path, {"action": "shutdown"}, 5)
    except (ConnectionRefusedError, FileNotFoundError):
        socket_path.unlink(missing_ok=True)
        return
    except (OSError, ValueError, RuntimeError):
        return
    for _ in range(400):
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
        "--backend",
        args.backend,
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
    if daemon_alive(
        args.socket,
        args.cwd,
        args.mode,
        args.backend,
        provider=PROVIDER,
        model=MODEL,
    ):
        return
    if args.socket.exists():
        stop_daemon(args.socket)
        if args.socket.exists():
            raise RuntimeError("previous controller did not stop; refusing another owner")
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
        if daemon_alive(
            args.socket,
            args.cwd,
            args.mode,
            args.backend,
            provider=PROVIDER,
            model=MODEL,
        ):
            return
        time.sleep(0.2)
    raise RuntimeError(f"DSH {args.mode} session daemon did not start; see {log_path}")


def client_main(args: argparse.Namespace) -> int:
    if args.show_ui_url and daemon_alive(args.socket, args.cwd, args.mode, args.backend,
                                        provider=PROVIDER, model=MODEL):
        return _client_show_ui_url(args)
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    with (args.runtime_dir / f"{args.mode}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"A DSH {args.mode} request is already running.", file=sys.stderr)
            return 75
        if args.show_ui_url:
            return _client_show_ui_url(args)
        return _client_dispatch_prompt(args)


def _client_show_ui_url(args: argparse.Namespace) -> int:
    ensure_daemon(args)
    response = exchange(args.socket, {"action": "ui-url"}, 90)
    if not response.get("ok"):
        print(
            f"DSH ui-url unavailable: {response.get('error', 'unknown error')}",
            file=sys.stderr,
        )
        return 1
    print(response.get("url", ""))
    return 0


def _client_dispatch_prompt(args: argparse.Namespace) -> int:
    prompt = args.prompt_file.read_text()
    if not prompt.strip():
        raise ValueError("prompt file is empty")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
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
        print(
            f"DSH request failed: {response.get('error', 'unknown error')}",
            file=sys.stderr,
        )
        return 1
    _emit_prompt_response_log(response, args)
    return 0



def _emit_prompt_response_log(response: dict[str, Any], args: argparse.Namespace) -> None:
    """Print the prompt-response summary lines to the operator's stderr."""
    if response.get("text"):
        print(response["text"])
    event = response.get("event") if isinstance(response.get("event"), dict) else {}
    if event.get("kind"):
        print(
            f"DSH session={response.get('session_key')} event={event.get('event_id')} "
            f"kind={event.get('kind')} sequence={event.get('sequence')}",
            file=sys.stderr,
        )
    _emit_ui_location(response)
    if response.get("action_required"):
        print(
            "DSH scout requested a terminal action handoff; the payload is scout-declared, "
            "not authorization. Verify independently before acting on it.",
            file=sys.stderr,
        )
    context = int(response.get("context_tokens", 0))
    window = int(response.get("context_window", 0))
    percent = round(context * 100 / window, 1) if window else 0
    print(
        f"DSH {args.mode} session={response.get('session_key')} turn={response.get('turns')} "
        f"context={context}/{window} ({percent}%) rotated={str(bool(response.get('rotated'))).lower()}",
        file=sys.stderr,
    )
    if response.get("verification_state") == "pending":
        print(
            f"DSH handoff completed but independent orchestrator verification is PENDING "
            f"for session={response.get('session_key')}; state file: {response.get('state_file')}",
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


def _emit_ui_location(response: dict[str, Any]) -> None:
    ui_url_payload = response.get("ui_url")
    if isinstance(ui_url_payload, dict) and ui_url_payload.get("url"):
        print(
            f"DSH scout UI: {ui_url_payload['url']} (authenticate with --show-ui-url)",
            file=sys.stderr,
        )


def parse_args() -> argparse.Namespace:
    parser = _build_argument_parser()
    args = parser.parse_args()
    args.cwd = args.cwd.resolve(strict=True)
    if args.backend is None:
        args.backend = resolve_backend()
    _apply_default_paths(args)
    _validate_client_args(parser, args)
    return args


def _build_argument_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--backend",
        choices=list(VALID_BACKENDS),
        default=None,
        help="execution backend; defaults to DSH_SCOUT_BACKEND (web)",
    )
    parser.add_argument(
        "--show-ui-url",
        action="store_true",
        help="print the scout-managed UI URL (web backend) and exit; does not dispatch a prompt",
    )
    return parser


def _apply_default_paths(args: argparse.Namespace) -> None:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"codex-dsh-agent-{os.getuid()}"
    state_root = CODEX_HOME / "state/dsh-scout"
    args.runtime_dir = runtime
    args.socket = args.socket or runtime / f"{args.mode}.sock"
    args.state_file = args.state_file or state_root / f"{args.mode}.json"


def _validate_client_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.serve:
        return
    if args.show_ui_url:
        _validate_show_ui_url_args(parser, args)
        return
    if args.prompt_file is None or not args.prompt_file.is_file():
        parser.error("--prompt-file must name an existing file")
    if not args.session_key or not args.session_key.strip():
        parser.error("--session-key is required")
    args.prompt_file = args.prompt_file.resolve(strict=True)


def _validate_show_ui_url_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.prompt_file is not None or args.session_key is not None or args.rotate:
        parser.error("--show-ui-url cannot be combined with --prompt-file/--session-key/--rotate")


def main() -> int:
    args = parse_args()
    if args.serve:
        args.runtime_dir.mkdir(parents=True, exist_ok=True)
        with (args.runtime_dir / f"{args.mode}.daemon.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return SessionDaemon(args.mode, args.cwd, args.socket, args.state_file,
                                 backend=args.backend).serve()
    return client_main(args)


if __name__ == "__main__":
    raise SystemExit(main())

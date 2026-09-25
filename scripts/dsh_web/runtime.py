"""Runtime process management: boot, reap, terminate, owner identity.

The runtime is started with ``--patch <own-settings> --profile web`` BEFORE
profile flags (the DSH CLI loads patches first). The controller persists an
``owner-identity`` JSON next to the runtime marker so a stale-reap scan can
verify the recorded process is actually ours (UID + pid starttime + cmdline
that contains the patch path) before sending any signal.

Per the review, every termination path is bounded by an explicit deadline;
the runtime record is only cleared AFTER a confirmed exit, never before.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import StartupError, TransportError
from .protocol import atomic_private_write, parse_runtime_url


RUNTIME_MARKER_FILENAME = "runtime-marker"
RUNTIME_RECORD_FILENAME = "runtime-record.json"

# Defaults for tests + normal use.
DEFAULT_STARTUP_TIMEOUT = 60.0
DEFAULT_TERMINATE_TIMEOUT = 5.0


# --- Marker + record persistence. -------------------------------------------


def runtime_marker_path(state_dir: Path, mode: str) -> Path:
    return state_dir / mode / RUNTIME_MARKER_FILENAME


def runtime_record_path(state_dir: Path, mode: str) -> Path:
    return state_dir / mode / RUNTIME_RECORD_FILENAME


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    atomic_private_write(path, json.dumps(payload, ensure_ascii=False))


def write_runtime_marker(state_dir: Path, mode: str) -> str:
    marker = runtime_marker_path(state_dir, mode)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(marker.parent, 0o700)
    except OSError:
        pass
    value = f"{os.getpid()}:{uuid.uuid4().hex}"
    marker.write_text(value)
    os.chmod(marker, 0o600)
    return value


def write_runtime_record(
    state_dir: Path,
    mode: str,
    *,
    pid: int,
    generation: int,
    patch_path: Path,
    settings_path: Path,
    owner_uid: int,
) -> None:
    record = runtime_record_path(state_dir, mode)
    record.parent.mkdir(parents=True, exist_ok=True)
    marker_file = runtime_marker_path(state_dir, mode)
    if marker_file.is_file():
        marker_value = marker_file.read_text().strip()
    else:
        marker_value = f"{pid}:{uuid.uuid4().hex}"
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        marker_file.write_text(marker_value)
        os.chmod(marker_file, 0o600)
    _atomic_write(
        record,
        {
            "pid": pid,
            "generation": generation,
            "marker": marker_value,
            "patch": str(patch_path),
            "settings": str(settings_path),
            "uid": owner_uid,
            "start_ticks": _proc_identity(pid)[0],
        },
    )


def clear_runtime_record(state_dir: Path, mode: str) -> None:
    record = runtime_record_path(state_dir, mode)
    try:
        record.unlink()
    except FileNotFoundError:
        pass


def read_runtime_record(state_dir: Path, mode: str) -> dict[str, Any] | None:
    record = runtime_record_path(state_dir, mode)
    if not record.is_file():
        return None
    try:
        value = json.loads(record.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# --- Stale-runtime reaping. --------------------------------------------------


def reap_stale_runtime(state_dir: Path, mode: str, uid: int) -> bool:
    """Kill a stale runtime only when it is provably ours.

    Returns True when something was reaped. A foreign process (wrong uid,
    different settings/patch path, dead pid) is left alone — ``kill`` is
    never issued against an unknown pid.
    """
    record = read_runtime_record(state_dir, mode)
    pid = _extract_pid(record)
    if pid is None:
        return False
    if not _marker_matches(state_dir, mode, record):
        return False
    if not _proc_belongs_to_us(pid, uid, record):
        return False
    stopped = _try_killpg(pid)
    if stopped:
        clear_runtime_record(state_dir, mode)
    return stopped


def _extract_pid(record: dict[str, Any] | None) -> int | None:
    if not isinstance(record, dict):
        return None
    pid = record.get("pid")
    if isinstance(pid, int) and pid > 0:
        return pid
    return None


def _marker_matches(state_dir: Path, mode: str, record: dict[str, Any]) -> bool:
    marker = runtime_marker_path(state_dir, mode)
    if not marker.is_file():
        return False
    try:
        own_marker = marker.read_text().strip()
    except OSError:
        return False
    return own_marker == record.get("marker")


def _proc_identity(pid: int) -> tuple[str, str]:
    """Linux start ticks distinguish a process from a later reuse of its PID."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19], fields[0]


def _proc_belongs_to_us(pid: int, uid: int, record: dict[str, Any]) -> bool:
    try:
        proc = Path(f"/proc/{pid}")
        argv = (proc / "cmdline").read_bytes().decode().split("\0")
        patch = record.get("patch")
        return bool(
            patch
            and record.get("uid") == uid
            and proc.stat().st_uid == uid
            and record.get("start_ticks") == _proc_identity(pid)[0]
            and os.getpgid(pid) == pid
            and any(argv[i : i + 2] == ["--patch", patch] for i in range(len(argv)))
        )
    except (OSError, ValueError, IndexError):
        return False


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _proc_identity(pid)[1] == "Z":
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.05)
    return False


def _try_killpg(pid: int) -> bool:
    for sig, timeout in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        if _wait_gone(pid, timeout):
            return True
    raise StartupError("owned stale runtime did not stop; refusing another runtime")


# --- Runtime process wrapper. ------------------------------------------------


def _resolve_dsh() -> str:
    path = shutil.which("dsh")
    if path is None:
        raise StartupError("dsh executable is not available on PATH")
    return path


class RuntimeProcess:
    """Owns one ``dsh web`` child process and its authenticated URL.

    The constructor seeds the per-mode settings/patch files; ``start`` boots
    the runtime, parses the announced URL, and persists the runtime record
    so a subsequent controller restart can verify ownership before reaping.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        mode: str,
        cwd: Path,
        user_home: Path,
        provider: str,
        model: str,
        host: str = "127.0.0.1",
        port: int | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        env: dict[str, str] | None = None,
        command_factory: Callable[[list[str], Path, dict[str, str]], subprocess.Popen]
        | None = None,
    ) -> None:
        from .settings import seed_settings

        self.state_dir = state_dir
        self.mode = mode
        self.cwd = cwd
        self.token: str | None = None
        self.origin: str | None = None
        self.launch_path: str | None = None
        self.url: str | None = None
        self.generation = 0
        self.process: subprocess.Popen | None = None
        self._startup_timeout = startup_timeout
        self._host = host
        self._port = port if port is not None else 0
        self._env = dict(env) if env is not None else {**os.environ}
        self._env["DSH_PERMISSION_MODE"] = "danger-full-access"
        self._env.pop("DISPLAY", None)
        self._command_factory = command_factory or self._default_command_factory
        self.settings_path, self.patch_path = seed_settings(
            state_dir=state_dir / mode,
            user_home=user_home,
            provider=provider,
            model=model,
        )

    # --- Process factory. ----------------------------------------------------

    def _default_command_factory(
        self, argv: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.Popen:
        # Drain both streams continuously; never persist the launch token.
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

    def _command(self) -> list[str]:
        return [
            _resolve_dsh(),
            "--patch",
            str(self.patch_path),
            "--profile",
            "web",
            "--host",
            self._host,
            "--port",
            str(self._port),
            "--no-open",
        ]

    # --- Lifecycle. ----------------------------------------------------------

    def start(self) -> str:
        write_runtime_marker(self.state_dir, self.mode)
        proc = self._command_factory(self._command(), self.cwd, self._env)
        self.process = proc
        self.generation += 1
        try:
            write_runtime_record(
                self.state_dir,
                self.mode,
                pid=proc.pid,
                generation=self.generation,
                patch_path=self.patch_path,
                settings_path=self.settings_path,
                owner_uid=os.getuid(),
            )
            url = self._wait_for_url(proc)
            self.origin, self.launch_path, self.token = parse_runtime_url(url)
            self.url = url
            return url
        except Exception:
            self._terminate()
            raise

    def _wait_for_url(self, proc: subprocess.Popen) -> str:
        if proc.stdout is None:
            raise StartupError("runtime stdout is not captured")
        announcements: queue.Queue = queue.Queue(maxsize=1)
        reader = threading.Thread(
            target=self._drain_output, args=(proc.stdout, announcements), daemon=True
        )
        reader.start()
        try:
            value = announcements.get(timeout=self._startup_timeout)
        except queue.Empty:
            raise StartupError(
                f"runtime did not announce a URL within {self._startup_timeout:g}s"
            ) from None
        if value is None:
            raise StartupError("runtime exited before announcing URL")
        return value

    def _drain_output(self, stdout: Any, announcements: queue.Queue) -> None:
        announced = False
        try:
            for line in stdout:
                # Startup diagnostics may contain private provider details. The
                # caller gets bounded error categories, not arbitrary child logs.
                if not announced and line.strip().startswith("dsh web: "):
                    announcements.put(line.strip()[len("dsh web: ") :].split(" ")[0])
                    announced = True
        finally:
            if not announced:
                announcements.put(None)
            stdout.close()

    def terminate(self) -> None:
        self._terminate()

    def _terminate(self, *, deadline: float = DEFAULT_TERMINATE_TIMEOUT) -> None:
        proc = self.process
        if proc is None:
            return
        for sig, timeout in ((signal.SIGTERM, deadline), (signal.SIGKILL, 2.0)):
            if proc.poll() is not None:
                break
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
        if proc.poll() is None:
            raise TransportError("owned runtime exit unconfirmed")
        if proc.stderr is not None:
            proc.stderr.close()
        self.process = None
        self.url = None
        self.token = None
        clear_runtime_record(self.state_dir, self.mode)

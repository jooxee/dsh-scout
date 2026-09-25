"""Tests for the ``dsh_web`` package and the controller ↔ web-backend boundary.

These tests exercise the *real* packaging shape (no test-shaped facades): the
package is imported as `import dsh_web`, sub-modules are exercised through the
public API, and the controller-side test class calls
``SessionDaemon.handle(...)`` against a fake ``dsh`` binary so the actual
prompt-dispatch path runs through ``WebBackend``.
"""

import base64
import hashlib
import http.cookiejar
import importlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
for path in (PACKAGE_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import dsh_web  # noqa: E402

CONTROLLER_PATH = SCRIPTS_DIR / "run_dsh_session.py"


def _import_controller():
    spec = importlib.util.spec_from_file_location("run_dsh_session", CONTROLLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = _import_controller()


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class ParseRuntimeUrlTests(unittest.TestCase):
    def test_extract_origin_path_token(self) -> None:
        origin, path, token = dsh_web.parse_runtime_url(
            "http://127.0.0.1:40481/?token=KI4s20b8"
        )
        self.assertEqual(origin, "http://127.0.0.1:40481")
        self.assertEqual(path, "/")
        self.assertEqual(token, "KI4s20b8")

    def test_no_token(self) -> None:
        origin, path, token = dsh_web.parse_runtime_url("http://127.0.0.1:40481/path")
        self.assertEqual(origin, "http://127.0.0.1:40481")
        self.assertEqual(path, "/path")
        self.assertIsNone(token)

    def test_invalid_scheme_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dsh_web.parse_runtime_url("file:///etc/passwd")


# ---------------------------------------------------------------------------
# Settings seeding (uses real DSH js-yaml)
# ---------------------------------------------------------------------------


def _yaml_load_via_dsh(path: Path) -> dict:
    from dsh_web.protocol import _yaml_safe_load

    return _yaml_safe_load(path.read_text())


class SettingsSeedTests(unittest.TestCase):
    def _path(self, temp: str) -> tuple[Path, Path, Path]:
        root = Path(temp)
        user_home = root / "user"
        user_home.mkdir()
        (user_home / "settings.yaml").write_text(
            "# comment\nproviders:\n  opencode-go:\n    baseUrl: https://example\n"
        )
        state = root / "state"
        return root, user_home, state

    def test_override_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, user_home, state = self._path(temp)
            settings, patch = dsh_web.seed_settings(
                state_dir=state,
                user_home=user_home,
                provider="opencode-go",
                model="glm-5.3-flash",
            )
            parsed = _yaml_load_via_dsh(settings)
            self.assertEqual(parsed["agent-default-model"]["provider"], "opencode-go")
            self.assertEqual(parsed["agent-default-model"]["model"], "glm-5.3-flash")
            self.assertEqual(
                parsed["providers"]["opencode-go"]["baseUrl"], "https://example"
            )
            self.assertTrue(patch.read_text().startswith("- id: settings"))

    def test_no_in_place_mutation_of_operator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, user_home, state = self._path(temp)
            before = (user_home / "settings.yaml").read_text()
            dsh_web.seed_settings(
                state_dir=state,
                user_home=user_home,
                provider="opencode-go",
                model="glm-5.3-flash",
            )
            self.assertEqual((user_home / "settings.yaml").read_text(), before)

    def test_missing_settings_creates_minimal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, user_home, state = self._path(temp)
            (user_home / "settings.yaml").unlink()
            settings, _ = dsh_web.seed_settings(
                state_dir=state,
                user_home=user_home,
                provider="opencode-go",
                model="glm-5.3-flash",
            )
            parsed = _yaml_load_via_dsh(settings)
            self.assertEqual(parsed["agent-default-model"]["provider"], "opencode-go")

    def test_path_with_special_chars_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, user_home, state = self._path(temp)
            strange_dir = state.parent / "strange'name:dir"
            strange_dir.mkdir(exist_ok=True)
            settings, patch = dsh_web.seed_settings(
                state_dir=strange_dir,
                user_home=user_home,
                provider="opencode-go",
                model="glm-5.3-flash",
            )
            content = patch.read_text()
            # Path is single-quoted (so YAML escapes don't break); the
            # inner single quote is doubled, the colon is literal.
            self.assertIn("strange''name:dir/scout-settings.yaml", content)
            self.assertIn("  path: '", content)
            # Round-trip via the DSH YAML parser to confirm single-quoted
            # path is parsed back as a plain scalar.
            parsed = _yaml_load_via_dsh(settings)
            self.assertEqual(parsed["agent-default-model"]["provider"], "opencode-go")

    def test_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, user_home, state = self._path(temp)
            settings, patch = dsh_web.seed_settings(
                state_dir=state,
                user_home=user_home,
                provider="opencode-go",
                model="glm-5.3-flash",
            )
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            self.assertEqual(patch.stat().st_mode & 0o777, 0o600)
            self.assertEqual(settings.parent.stat().st_mode & 0o777, 0o700)


# ---------------------------------------------------------------------------
# WS codec (loopback)
# ---------------------------------------------------------------------------


def _unmasked_server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


def _run_once_server(response: bytes, *, cookies_header: str | None = None) -> int:
    """Serve ``response`` (the post-upgrade part) after a 101 handshake.

    Returns the listening port.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    captured = []

    def loop() -> None:
        try:
            conn, _ = server.accept()
            conn.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            assert b" 101 " not in head  # not used here
            headers = [
                "HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket",
                "Connection: Upgrade",
            ]
            conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
            conn.sendall(response)
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    captured.append(chunk)
            except OSError:
                pass
        except OSError:
            pass

    threading.Thread(target=loop, daemon=True).start()
    return port


def _make_ws_accept_header(key: str) -> str:
    import base64
    import hashlib

    digest = hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
    ).digest()
    return "Sec-WebSocket-Accept: " + base64.b64encode(digest).decode()


def _accept_one(
    handshake_response: bytes, *, ready: threading.Event | None = None
) -> tuple[socket.socket, int, dict]:
    """Accept one TCP connection and respond with ``handshake_response``.

    If the response is just a partial header (no ``Sec-WebSocket-Accept``),
    we compute and append the correct value here so the driver's acceptance
    check passes.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(5)
    port = server.getsockname()[1]
    holder = {"conn": None, "data": b""}
    ready_event = ready if ready is not None else threading.Event()

    def loop() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            ready_event.set()
            return
        conn.settimeout(3)
        data = b""
        while b"\r\n\r\n" not in data:
            try:
                chunk = conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        # Compute Sec-WebSocket-Accept if the response carries one.
        lines = data.decode("utf-8", errors="replace").split("\r\n")
        key = ""
        for line in lines[1:]:
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break
        if (
            b"Sec-WebSocket-Accept" in handshake_response
            or b"sec-websocket-accept" in handshake_response.lower()
        ):
            response = handshake_response
        else:
            response = (
                handshake_response + _make_ws_accept_header(key).encode() + b"\r\n"
            )
            if not response.endswith(b"\r\n\r\n"):
                response = response.rstrip(b"\r\n") + b"\r\n\r\n"
        try:
            conn.sendall(response)
        except OSError:
            pass
        holder["conn"] = conn
        holder["data"] = data
        ready_event.set()

    threading.Thread(target=loop, daemon=True).start()
    return server, port, holder  # type: ignore[return-value]


class WsCodecTests(unittest.TestCase):
    def test_handshake_round_trip(self) -> None:
        # Server sends the canonical accept response; driver should accept.
        from dsh_web.ws import WsConn

        ready = threading.Event()
        server, port, holder = _accept_one(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n",
            ready=ready,
        )
        try:
            ws = WsConn("127.0.0.1", port, "/", "dsh-scout=sess", "http://127.0.0.1:0")
            # Loop runs in a thread; give it 200ms to populate holder if not done.
            if not holder["data"]:
                ready.wait(0.5)
            request_lines = (
                holder["data"].decode("utf-8", errors="replace").split("\r\n")
            )
            self.assertIn("GET / HTTP/1.1", request_lines, msg=str(holder["data"]))
            ws.send_text(json.dumps({"ping": "ok"}))
            ws.close()
        finally:
            try:
                server.close()
                if holder["conn"] is not None:
                    holder["conn"].close()
            except OSError:
                pass

    def test_handshake_rejected(self) -> None:
        from dsh_web.ws import WsConn

        server, port, holder = _accept_one(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        try:
            with self.assertRaises(dsh_web.AuthError):
                WsConn("127.0.0.1", port, "/", "dsh-scout=sess", "http://127.0.0.1:0")
        finally:
            try:
                server.close()
                if holder["conn"] is not None:
                    holder["conn"].close()
            except OSError:
                pass

    def test_ping_pong(self) -> None:
        from dsh_web.ws import WsConn

        # Server sends a PING, then a text frame; driver must respond to the
        # PING and yield the text frame.
        payload = json.dumps({"hello": "world"})
        response_body = _unmasked_server_frame(0x9, b"ping") + _unmasked_server_frame(
            0x1, payload.encode()
        )
        server, port, holder = _accept_one(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        )
        # After handshake, send the queued frames from the calling thread.
        threading.Thread(
            target=lambda: (
                time.sleep(0.05),
                holder["conn"].sendall(response_body),
            ),
            daemon=True,
        ).start()
        try:
            ws = WsConn("127.0.0.1", port, "/", "dsh-scout=sess", "http://127.0.0.1:0")
            msg = ws.read_message(deadline=time.monotonic() + 2)
            self.assertEqual(msg, payload)
            ws.close()
        finally:
            try:
                server.close()
                if holder["conn"] is not None:
                    holder["conn"].close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# RPC envelope + HostClient over loopback HTTP
# ---------------------------------------------------------------------------


def _start_http(handler) -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]

    def loop() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return port


def _read_request(conn: socket.socket) -> dict:
    conn.settimeout(3)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", errors="replace").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    content_length = int(headers.get("content-length", "0"))
    while len(body) < content_length:
        chunk = conn.recv(content_length - len(body))
        if not chunk:
            break
        body += chunk
    body_json = json.loads(body) if body else None
    return {
        "method": method,
        "path": path,
        "headers": headers,
        "body": body_json,
    }


def _send_response(
    conn: socket.socket,
    status: int,
    *,
    payload: dict | None = None,
    cookie: str | None = None,
    length: int | None = None,
) -> None:
    if payload is not None:
        body = json.dumps(payload).encode()
        length = len(body)
        content_type = "application/json"
    else:
        body = b""
        length = 0
        content_type = "text/plain"
    headers = [
        f"HTTP/1.1 {status} Status",
        f"Content-Length: {length}",
        f"Content-Type: {content_type}",
    ]
    if cookie:
        headers.append("Set-Cookie: dsh-scout=sess; Path=/; HttpOnly")
    conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + body)


class HostClientTests(unittest.TestCase):
    def test_envelope_shape_and_rpc_id(self) -> None:
        seen: list[dict] = []

        def handler(conn: socket.socket) -> None:
            req = _read_request(conn)
            seen.append({"path": req["path"], "body": req["body"]})
            value = {"ok": True, "value": {"echo": req["body"]["payload"]["args"]}}
            _send_response(
                conn,
                200,
                payload={
                    "type": "server-response",
                    "rpcId": req["body"]["rpcId"],
                    "result": value,
                },
            )

        port = _start_http(handler)
        jar = http.cookiejar.CookieJar()
        cookie = http.cookiejar.Cookie(
            version=0,
            name="dsh-scout",
            value="sess",
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
        client = dsh_web.HostClient(f"http://127.0.0.1:{port}", jar, timeout=3)
        result = client.call(
            "session/follow",
            {"request": {"address": {"kind": "session", "sessionId": "s"}}},
        )
        self.assertEqual(
            result,
            {"echo": {"request": {"address": {"kind": "session", "sessionId": "s"}}}},
        )
        self.assertEqual(seen[0]["path"], "/api/session/follow")
        self.assertEqual(seen[0]["body"]["type"], "client-request")
        self.assertEqual(seen[0]["body"]["method"], "session/follow")

    def test_token_scrubbing_on_error(self) -> None:
        captured: list[dict] = []

        def handler(conn: socket.socket) -> None:
            req = _read_request(conn)
            captured.append(req)
            _send_response(
                conn,
                200,
                payload={
                    "type": "server-response",
                    "rpcId": req["body"]["rpcId"],
                    "result": {
                        "ok": False,
                        "error": {
                            "code": "EPERM",
                            "message": "KI4s20b8I7gbe_whMgmYbmPuNhCXv_dV-QgfxwUBk40",
                        },
                    },
                },
            )

        port = _start_http(handler)
        jar = http.cookiejar.CookieJar()
        cookie = http.cookiejar.Cookie(
            version=0,
            name="dsh-scout",
            value="sess",
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
        client = dsh_web.HostClient(
            f"http://127.0.0.1:{port}",
            jar,
            timeout=3,
            token="KI4s20b8I7gbe_whMgmYbmPuNhCXv_dV-QgfxwUBk40",
        )
        with self.assertRaises(dsh_web.WebBackendError) as ctx:
            client.call("session/prompt", {"request": {}})
        self.assertIn("***", str(ctx.exception))
        self.assertNotIn(
            "KI4s20b8I7gbe_whMgmYbmPuNhCXv_dV-QgfxwUBk40", str(ctx.exception)
        )

    def test_auth_401(self) -> None:
        def handler(conn: socket.socket) -> None:
            req = _read_request(conn)
            _send_response(
                conn,
                200,
                payload={
                    "type": "server-response",
                    "rpcId": req["body"]["rpcId"],
                    "result": {
                        "ok": False,
                        "error": {"code": "ENOAUTH", "message": "no"},
                    },
                },
            )

        port = _start_http(handler)
        jar = http.cookiejar.CookieJar()
        cookie = http.cookiejar.Cookie(
            version=0,
            name="dsh-scout",
            value="sess",
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
        client = dsh_web.HostClient(f"http://127.0.0.1:{port}", jar, timeout=3)
        with self.assertRaises(dsh_web.WebBackendError):
            client.call("session/prompt", {"request": {}})

    def test_protocol_error_on_non_json(self) -> None:
        def handler(conn: socket.socket) -> None:
            _read_request(conn)
            _send_response(conn, 200, payload={"type": "something-else", "result": {}})

        port = _start_http(handler)
        jar = http.cookiejar.CookieJar()
        cookie = http.cookiejar.Cookie(
            version=0,
            name="dsh-scout",
            value="sess",
            port=None,
            port_specified=False,
            domain="127.0.0.1",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
        client = dsh_web.HostClient(f"http://127.0.0.1:{port}", jar, timeout=3)
        with self.assertRaises(dsh_web.ProtocolError):
            client.call("session/prompt", {"request": {}})


# ---------------------------------------------------------------------------
# Runtime lifecycle
# ---------------------------------------------------------------------------


class RuntimeStartupTests(unittest.TestCase):
    def test_no_dsh_binary(self) -> None:
        # Strip only `dsh` from PATH (keep `node` so seed_settings works).
        sanitized = {k: v for k, v in os.environ.items() if k != "PATH"}
        sanitized["PATH"] = "/nonexistent-bin-dir-for-this-test"
        with mock.patch.dict(os.environ, sanitized, clear=True):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                user_home = root / "user"
                user_home.mkdir()
                # Skip js-yaml: write a minimal settings file so we don't
                # even reach the parser when the only thing we want to test
                # is that ``start()`` cannot find ``dsh``.
                # We short-circuit by going through a stub constructor path.
                real_seed = dsh_web.seed_settings

                def stub_seed(*args, **kwargs):
                    state_dir = kwargs.get("state_dir") or args[0]
                    state = Path(state_dir) / "scout-settings.yaml"
                    state.parent.mkdir(parents=True, exist_ok=True)
                    state.write_text(
                        "agent-default-model:\n  provider: opencode-go\n  model: glm-5.3-flash\n"
                    )
                    patch = state.parent / "web-patch.yml"
                    patch.write_text("- id: settings\n  config:\n    path: x\n")
                    return state, patch

                dsh_web.settings.seed_settings = stub_seed  # type: ignore[attr-defined]
                try:
                    cwd = root / "cwd"
                    cwd.mkdir()
                    runtime = dsh_web.RuntimeProcess(
                        state_dir=root / "state",
                        mode="web",
                        cwd=cwd,
                        user_home=user_home,
                        provider="opencode-go",
                        model="glm-5.3-flash",
                    )
                    with self.assertRaises(dsh_web.StartupError):
                        runtime.start()
                finally:
                    dsh_web.settings.seed_settings = real_seed  # type: ignore[attr-defined]

    def test_writes_record_after_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            user_home = root / "user"
            user_home.mkdir()
            (user_home / "settings.yaml").write_text("")
            cwd = root / "cwd"
            cwd.mkdir()
            state = root / "state"

            class FakeFactory:
                @staticmethod
                def _factory(argv, cwd_path, env):
                    host_flag = argv[argv.index("--host") + 1]
                    line = f"printf 'dsh web: http://{host_flag}:1/?token=abcdefgh\\n'"
                    return subprocess.Popen(
                        ["/bin/sh", "-c", line + " && sleep 30"],
                        cwd=str(cwd_path),
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )

            runtime = dsh_web.RuntimeProcess(
                state_dir=state,
                mode="web",
                cwd=cwd,
                user_home=user_home,
                provider="opencode-go",
                model="glm-5.3-flash",
                port=1,
                startup_timeout=2,
                command_factory=FakeFactory._factory,
            )
            try:
                with mock.patch(
                    "dsh_web.runtime._resolve_dsh", return_value="test-dsh"
                ):
                    url = runtime.start()
                self.assertIsNotNone(runtime.token)
                self.assertEqual(url, f"http://{runtime._host}:1/?token=abcdefgh")
                # Marker + record written
                self.assertTrue(dsh_web.runtime_marker_path(state, "web").exists())
                self.assertTrue(dsh_web.runtime_record_path(state, "web").exists())
            finally:
                runtime.terminate()


# ---------------------------------------------------------------------------
# Backend dispatch (controller config)
# ---------------------------------------------------------------------------


class BackendDispatchTests(unittest.TestCase):
    def test_default_backend_is_web(self) -> None:
        env = os.environ.copy()
        env.pop("DSH_SCOUT_BACKEND", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(controller.resolve_backend(), "web")

    def test_sdk_is_acceptable(self) -> None:
        os.environ["DSH_SCOUT_BACKEND"] = "sdk"
        try:
            self.assertEqual(controller.resolve_backend(), "sdk")
        finally:
            os.environ.pop("DSH_SCOUT_BACKEND", None)

    def test_invalid_backend_rejected(self) -> None:
        os.environ["DSH_SCOUT_BACKEND"] = "garbage"
        try:
            with self.assertRaises(ValueError):
                controller.resolve_backend()
        finally:
            os.environ.pop("DSH_SCOUT_BACKEND", None)


# ---------------------------------------------------------------------------
# Controller ↔ backend integration: uses real SessionDaemon.handle and a real
# in-process fake ``dsh web`` (an HTTP server) so the entire dispatch path
# runs through ``WebBackend`` + ``TurnDriver``.
# ---------------------------------------------------------------------------


class FakeLoopbackDsh:
    """A minimal in-process ``dsh web`` - look-alike.

    Implements just enough of the protocol to drive ``TurnDriver`` through
    one complete turn. Driven by individual tests via ``set_*`` mutators.
    """

    def __init__(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.host = "127.0.0.1"
        self.port = self.server.getsockname()[1]
        self.events_log: list[dict] = []
        self._next_rpc = 1
        self._ws = []
        self._stop = threading.Event()
        self._sessions: dict[str, dict] = {}
        self._answer = "PONG"
        self._hold_until_session_cancel = threading.Event()
        self._follows: list[tuple[socket.socket, str]] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def set_answer(self, text: str) -> None:
        self._answer = text

    def hold_until_cancel(self) -> None:
        self._hold_until_session_cancel.clear()

    def release_hold(self) -> None:
        self._hold_until_session_cancel.set()

    # --- internals. ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_one, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> dict | None:
        conn.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", errors="replace").split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
        content_length = int(headers.get("content-length", "0"))
        while len(body) < content_length:
            chunk = conn.recv(content_length - len(body))
            if not chunk:
                break
            body += chunk
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = None
        return {
            "method": method,
            "path": path,
            "headers": headers,
            "body": payload,
            "raw": data,
            "conn": conn,
        }

    def _respond(
        self,
        conn: socket.socket,
        status: int,
        payload: dict | None = None,
        raw: bool = False,
        ws: bool = False,
    ) -> None:
        if ws:
            conn.sendall(payload["raw"])  # type: ignore[index]
            return
        if payload is None or raw:
            assert payload is not None
            conn.sendall(payload["raw"])  # type: ignore[index]
            return
        body = json.dumps(payload).encode()
        status_line = {
            200: "200 OK",
            401: "401 Unauthorized",
            403: "403 Forbidden",
        }.get(status, f"{status} Status")
        if status in (200,):
            headers = [
                f"HTTP/1.1 {status_line}",
                f"Content-Length: {len(body)}",
                "Content-Type: application/json",
                "Set-Cookie: dsh-scout=sess; Path=/; HttpOnly",
            ]
        else:
            headers = [f"HTTP/1.1 {status_line}", "Content-Length: 0"]
        conn.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + body)

    def _serve_one(self, conn: socket.socket) -> None:
        try:
            req = self._read(conn)
            if req is None:
                conn.close()
                return
            path = req["path"]
            body = req["body"]
            if (
                path == "/"
                or path == "/?token=KI4s20b8I7gbe_whMgmYbmPuNhCXv_dV-QgfxwUBk40"
            ):
                # Cookie exchange — acknowledge with the session cookie.
                self._respond(conn, 200, payload={"ok": True})
                conn.close()
                return
            if path.startswith("/api/") and path != "/api/remote.mux":
                endpoint = path[len("/api/") :]
                rpc_id = (body or {}).get("rpcId") if isinstance(body, dict) else None
                response = self._handle_rpc(endpoint, body or {})
                self._respond(
                    conn,
                    200,
                    payload={
                        "type": "server-response",
                        "rpcId": rpc_id,
                        "result": response,
                    },
                )
                conn.close()
                return
            if path == "/api/remote.mux":
                # WS upgrade — keep connection open; the WS pump closes it
                # via ``_stop`` (test teardown).
                self._serve_websocket(conn, req)
                return
            conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            conn.close()
        except (OSError, ValueError):
            try:
                conn.close()
            except OSError:
                pass

    def _handle_rpc(self, endpoint: str, body: dict) -> dict:
        # The client-request payload is shaped as
        #   {"type": "client-request", "payload": {"args": <actual args>}}
        args = (body or {}).get("payload", {}).get("args", {})
        if endpoint == "workspace/create":
            self.cwd = args["request"]["path"]
            return {"ok": True, "value": {"workspace": {"workspaceId": "ws-1"}}}
        if endpoint == "session/create":
            session_id = args.get("request", {}).get("sessionId")
            self._sessions.setdefault(session_id, {"turn": 0})
            return {"ok": True, "value": {"sessionId": session_id}}
        if endpoint == "session/rename":
            return {"ok": True, "value": {"title": "ok"}}
        if endpoint == "session/prompt":
            session_id = args.get("request", {}).get("sessionId")
            self._sessions.setdefault(session_id, {"turn": 0})
            self._sessions[session_id]["turn"] += 1
            turn = self._sessions[session_id]["turn"]
            threading.Thread(
                target=self._stream_turn, args=(session_id, turn), daemon=True
            ).start()
            return {"ok": True, "value": {"queued": True}}
        if endpoint == "session/cancel":
            if self._hold_until_session_cancel.is_set():
                return {"ok": True, "value": {"accepted": False}}
            self.release_hold()
            return {"ok": True, "value": {"accepted": True}}
        return {"ok": False, "error": {"code": "ENOSYS", "message": endpoint}}

    def _stream_turn(self, session_id: str, turn: int) -> None:
        for sock, sid in self._follows:
            try:
                sock.sendall(
                    _ws_unmasked_frame(
                        json.dumps(
                            {
                                "type": "item",
                                "streamId": sid,
                                "value": {
                                    "type": "event",
                                    "event": {
                                        "type": "turn/start",
                                        "data": {"turn": turn},
                                    },
                                },
                            }
                        ).encode()
                    )
                )
                sock.sendall(
                    _ws_unmasked_frame(
                        json.dumps(
                            {
                                "type": "item",
                                "streamId": sid,
                                "value": {
                                    "type": "event",
                                    "event": {
                                        "type": "assistant/message",
                                        "data": {
                                            "turn": turn,
                                            "message": {
                                                "content": [
                                                    {
                                                        "type": "text",
                                                        "text": self._answer,
                                                    }
                                                ]
                                            },
                                        },
                                    },
                                },
                            }
                        ).encode()
                    )
                )
                sock.sendall(
                    _ws_unmasked_frame(
                        json.dumps(
                            {
                                "type": "item",
                                "streamId": sid,
                                "value": {
                                    "type": "event",
                                    "event": {
                                        "type": "turn/end",
                                        "data": {
                                            "turn": turn,
                                            "reason": {"kind": "completed"},
                                        },
                                    },
                                },
                            }
                        ).encode()
                    )
                )
            except OSError:
                pass

    def _serve_websocket(self, conn: socket.socket, req: dict) -> bool:
        # Read body handshake complete; write 101 with a valid accept key.
        head_text = req["raw"].decode("utf-8", errors="replace")
        key = ""
        for line in head_text.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        digest = hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()
        accept = base64.b64encode(digest).decode()
        conn.sendall(
            (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
            )
        )
        first = self._read_ws_frame(conn)
        if first is None:
            return False
        stream_id = first["payload"].get("streamId", "")
        self._follows.append((conn, stream_id))
        try:
            conn.sendall(
                _ws_unmasked_frame(
                    json.dumps(
                        {
                            "type": "item",
                            "streamId": stream_id,
                            "value": {
                                "type": "snapshot",
                                "header": {"cwd": self.cwd},
                                "cursor": 0,
                            },
                        }
                    ).encode()
                )
            )
        except OSError:
            pass
        return True

    def _read_ws_frame(self, conn: socket.socket) -> dict | None:
        # Client frames are masked.
        head = b""
        while len(head) < 2:
            chunk = conn.recv(2 - len(head))
            if not chunk:
                return None
            head += chunk
        b1, b2 = head[0], head[1]
        n = b2 & 0x7F
        if n == 126:
            ext = conn.recv(2)
            (n,) = struct.unpack(">H", ext)
        elif n == 127:
            ext = conn.recv(8)
            (n,) = struct.unpack(">Q", ext)
        mask = conn.recv(4)
        payload = b""
        while len(payload) < n:
            chunk = conn.recv(n - len(payload))
            if not chunk:
                break
            payload += chunk
        unmasked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        try:
            payload_obj = json.loads(unmasked)
        except json.JSONDecodeError:
            payload_obj = {}
        return {"opcode": b1 & 0x0F, "payload": payload_obj}


def _ws_unmasked_frame(payload: bytes) -> bytes:
    return _unmasked_server_frame(0x1, payload)


class ControllerWebIntegrationTests(unittest.TestCase):
    """Real ``SessionDaemon.handle(...)`` against an in-process web backend."""

    def _boot_daemon(self, fake: FakeLoopbackDsh):
        SessionDaemon = controller.SessionDaemon
        cwd = self.root
        socket_path = self.root / "dsh.sock"
        state_path = self.root / "state.json"
        if socket_path.exists():
            socket_path.unlink()
        daemon = SessionDaemon.__new__(SessionDaemon)
        daemon.__init__(  # type: ignore[misc]
            mode="write",
            cwd=cwd,
            socket_path=socket_path,
            state_path=state_path,
            backend="web",
        )
        # Replace the runtime with one driven by our fake loopback.
        from dsh_web.backend import WebBackend

        class _LoopbackRuntime:
            pass

        # The daemon's ``_ensure_web`` will lazy-init; instead, install a
        # pre-built WebBackend pointed at the loopback fake.
        backend = WebBackend(
            state_dir=self.root / "state",
            mode="web",
            cwd=self.root,
            user_home=self.root,
            provider="opencode-go",
            model="glm-5.3-flash",
            host=fake.host,
            port=fake.port,
            startup_timeout=2,
            socket_timeout=5,
        )
        # Skip the real start and wire origin/token directly.
        backend.runtime.token = "KI4s20b8I7gbe_whMgmYbmPuNhCXv_dV-QgfxwUBk40"
        backend.runtime.origin = fake.origin
        backend.runtime.launch_path = "/"
        backend.runtime.url = (
            f"{fake.origin}/?token=KI4s20b8I7gbe_whMgmYbmPuNhCXv_dV-QgfxwUBk40"
        )
        backend.runtime.generation = 1
        import http.cookiejar

        jar = http.cookiejar.CookieJar()
        cookie = http.cookiejar.Cookie(
            version=0,
            name="dsh-scout",
            value="sess",
            port=None,
            port_specified=False,
            domain=fake.host,
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
        from dsh_web.rpc import HostClient

        backend.client = HostClient(
            fake.origin, jar, timeout=5, token=backend.runtime.token
        )
        from dsh_web.runtime import write_runtime_record

        write_runtime_record(
            self.root / "state",
            "web",
            pid=os.getpid(),
            generation=1,
            patch_path=Path(self.root / "fake-patch.yml"),
            settings_path=Path(self.root / "fake-settings.yml"),
            owner_uid=os.getuid(),
        )
        # Patch the daemon's _ensure_web to return our backend.
        daemon.web = backend

        def _ensure():
            return daemon.web

        daemon._ensure_web = _ensure  # type: ignore[assignment]
        self._daemon = daemon
        return backend

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_handle_dispatches_prompt_through_real_web_backend(self) -> None:
        fake = FakeLoopbackDsh()
        backend = self._boot_daemon(fake)
        try:
            with mock.patch.object(
                controller,
                "wait_session_stats",
                return_value={"context_tokens": 10, "context_window": 1000},
            ):
                response = self._daemon.handle(
                    {
                        "action": "prompt",
                        "session_key": "dsh-scout:web-integration:writer",
                        "prompt": "Reply with exactly PONG",
                        "timeout": 30,
                    }
                )
            self.assertTrue(response.get("ok"), response)
            self.assertEqual(response.get("text"), "PONG")
            self.assertEqual(response.get("backend"), "web")
            ui = response.get("ui_url") or {}
            self.assertTrue(ui.get("url"), response)
            state_text = (self.root / "state.json").read_text()
            self.assertNotIn('"token"', state_text)
        finally:
            try:
                backend.terminate()
            except Exception:
                pass
            fake._stop.set()
            fake.server.close()
            for conn, _ in fake._follows:
                conn.close()

    def test_followup_reuses_history_and_rotation_replaces_live_session(self) -> None:
        fake = FakeLoopbackDsh()
        backend = self._boot_daemon(fake)
        request = {
            "action": "prompt",
            "session_key": "rotation",
            "prompt": "PONG",
            "timeout": 5,
        }
        try:
            with (
                mock.patch.object(
                    controller,
                    "wait_session_stats",
                    return_value={"context_tokens": 10},
                ),
                mock.patch.object(
                    controller, "repository_facts", return_value={"available": False}
                ),
            ):
                first = self._daemon.handle(request)
                second = self._daemon.handle(request)
                third = self._daemon.handle({**request, "rotate": True})
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertEqual(second["turns"], 2)
            self.assertNotEqual(second["session_id"], third["session_id"])
            self.assertTrue(third["rotated"])
            saved = self._daemon.sessions["rotation"]
            self.assertEqual(saved["session_id"], third["session_id"])
            self.assertEqual(saved["rotated_from"], first["session_id"])
            self.assertEqual(
                saved["verification_facts"]["pre_prompt"], {"available": False}
            )
        finally:
            backend.terminate()
            fake._stop.set()
            fake.server.close()
            for conn, _ in fake._follows:
                conn.close()

    def test_handle_records_failure_for_unconfirmed_cancel(self) -> None:
        from dsh_web.driver import TurnDriver
        from dsh_web.errors import CancellationUnconfirmed

        fake = FakeLoopbackDsh()
        backend = self._boot_daemon(fake)
        try:
            with mock.patch.object(
                TurnDriver,
                "run_prompt",
                side_effect=CancellationUnconfirmed("cancel rejected"),
            ):
                with self.assertRaises(RuntimeError):
                    self._daemon.handle(
                        {
                            "action": "prompt",
                            "session_key": "dsh-scout:cancel:writer",
                            "prompt": "prompt",
                            "timeout": 5,
                        }
                    )
        finally:
            try:
                backend.terminate()
            except Exception:
                pass
            fake._stop.set()
            fake.server.close()
            for conn, _ in fake._follows:
                conn.close()
        state = json.loads((self.root / "state.json").read_text())
        self.assertIn("sessions", state)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

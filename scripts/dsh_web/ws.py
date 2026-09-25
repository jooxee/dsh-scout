"""Minimal RFC6455 WebSocket client used to drive ``session/follow``.

Three correctness rules implemented here:

1. **Bounded absolute deadline** — every ``recv`` runs under a deadline and
   the connection is closed on expiry. Partial frames cannot outlive the
   deadline; the driver can decide whether to give up.
2. **Buffered frames are drained** — when ``select`` says the socket is
   readable we read everything currently available before going back to
   ``select``, so a partial frame followed by FIN does not deadlock.
3. **Fail fast** — a peer close (FIN without WS close frame) or a malformed
   frame raises ``TransportError`` immediately; the controller decides
   whether to cancel or terminate.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import select
import socket
import struct
import threading
import time

from .errors import AuthError, ProtocolError, TransportError
from .protocol import WS_FRAME_MAX_BYTES, WS_HANDSHAKE_TIMEOUT


class WsConn:
    """One WebSocket connection, single-threaded reader + thread-safe send."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        cookie_header: str,
        origin: str,
        *,
        handshake_timeout: float = WS_HANDSHAKE_TIMEOUT,
        frame_max_bytes: int = WS_FRAME_MAX_BYTES,
    ) -> None:
        self.frame_max_bytes = frame_max_bytes
        self.sock = socket.create_connection((host, port), timeout=handshake_timeout)
        self.buffer = b""
        try:
            self._complete_handshake(
                host, port, path, cookie_header, origin, handshake_timeout
            )
        except Exception:
            self._close_socket()
            raise
        self.send_lock = threading.Lock()
        self.closed = False

    # --- Handshake. -----------------------------------------------------------

    def _complete_handshake(
        self,
        host: str,
        port: int,
        path: str,
        cookie_header: str,
        origin: str,
        timeout: float,
    ) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n"
            f"Cookie: {cookie_header}\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode())
        self.sock.settimeout(timeout)
        data = b""
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in data:
            remaining = max(0.05, deadline - time.monotonic())
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                self._close_socket()
                raise TransportError("ws handshake: connection closed")
            data += chunk
            if len(data) > 16384:
                raise ProtocolError("oversized WebSocket handshake")
            if time.monotonic() >= deadline:
                self._close_socket()
                raise TransportError("ws handshake: timeout before headers complete")
        head, _, rest = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in f" {status} ":
            self._close_socket()
            raise AuthError(f"ws handshake rejected: {status}")
        expect = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        headers = dict(
            line.split(b":", 1) for line in head.split(b"\r\n")[1:] if b":" in line
        )
        accepted = {k.strip().lower(): v.strip() for k, v in headers.items()}.get(
            b"sec-websocket-accept"
        )
        if accepted != expect.encode():
            self._close_socket()
            raise AuthError("ws handshake: bad accept key")
        self.buffer = rest
        self.sock.settimeout(None)

    # --- Sending. -------------------------------------------------------------

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        mask = secrets.token_bytes(4)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(bytes(header) + mask + masked)

    def _send_control(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            return
        mask = secrets.token_bytes(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            with self.send_lock:
                self.sock.sendall(
                    bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked
                )
        except OSError:
            self.closed = True

    # --- Receiving. -----------------------------------------------------------

    def read_message(self, deadline: float | None = None) -> str | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            fin, opcode, size = self._read_frame_header(deadline)
            payload = self._read_payload(size, deadline)
            if opcode == 8:
                self.closed = True
                return None
            if opcode >= 9:
                if opcode == 9:
                    self._send_control(10, payload)
                continue
            if opcode not in (0, 1) or (opcode == 0) != bool(chunks):
                raise ProtocolError("unexpected WebSocket data frame")
            chunks.append(payload)
            total += size
            if total > self.frame_max_bytes:
                raise ProtocolError("WebSocket message exceeds bounded size")
            if fin:
                return self._decode_text(b"".join(chunks))

    @staticmethod
    def _decode_text(payload: bytes) -> str:
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("WebSocket message is not UTF-8") from None

    def _read_frame_header(self, deadline: float | None) -> tuple[bool, int, int]:
        b1, b2 = self._recv_exact(2, deadline)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        if b1 & 0x70 or b2 & 0x80:
            raise ProtocolError("reserved bits or masked server frame")
        size = b2 & 0x7F
        if size == 126:
            size = struct.unpack(">H", self._recv_exact(2, deadline))[0]
        elif size == 127:
            size = struct.unpack(">Q", self._recv_exact(8, deadline))[0]
        if size > self.frame_max_bytes:
            raise TransportError("WebSocket frame exceeds bounded size")
        if opcode >= 8 and (opcode not in (8, 9, 10) or not fin or size > 125):
            raise ProtocolError("invalid WebSocket control frame")
        return fin, opcode, size

    def _read_payload(self, n: int, deadline: float | None) -> bytes:
        return self._recv_exact(n, deadline)

    def _recv_exact(self, n: int, deadline: float | None) -> bytes:
        """Read exactly ``n`` bytes, bounded by ``deadline``.

        Always drains the socket of all currently readable data first so a
        FIN arriving after a partial payload still surfaces immediately.
        """
        if deadline is not None:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
        while len(self.buffer) < n:
            self._fill_buffer(deadline)
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def _fill_buffer(self, deadline: float | None) -> None:
        """Pull all readable bytes into the buffer; bounded by ``deadline``."""
        if deadline is not None and time.monotonic() >= deadline:
            raise TransportError("ws read deadline exceeded")
        try:
            r, _, _ = select.select([self.sock], [], [], 0.25)
        except (OSError, ValueError):
            raise TransportError("ws select failed")
        if not r:
            return
        try:
            chunk = self.sock.recv(65536)
        except (socket.timeout, OSError):
            chunk = b""
        if not chunk:
            raise TransportError("ws closed by peer")
        self.buffer += chunk
        if len(self.buffer) > self.frame_max_bytes + 4096:
            raise TransportError("ws frame payload exceeds bounded size")

    # --- Lifecycle. -----------------------------------------------------------

    def close(self) -> None:
        self.sock.settimeout(0.2)
        if not self.closed:
            self._send_control(0x8, b"\x03\xe8")
        self.closed = True
        self._close_socket()

    def _close_socket(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

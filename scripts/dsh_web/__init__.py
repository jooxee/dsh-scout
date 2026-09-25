#!/usr/bin/env python3
"""Scout-managed ordinary DSH web runtime backend (Issue #6).

Public entry points for the session controller and focused integration tests.
"""

from .errors import (
    AuthError,
    CancellationUnconfirmed,
    ProtocolError,
    StartupError,
    TransportError,
    WebBackendError,
)
from .settings import seed_settings
from .protocol import (
    IDENTITY_LIMIT_BYTES,
    IDENTITY_PREFIX,
    TITLE_LIMIT_BYTES,
    DETAIL_LIMIT,
    WS_FRAME_MAX_BYTES,
    WS_HANDSHAKE_TIMEOUT,
    clean_title_text,
    identity_line,
    parse_runtime_url,
    parse_ui_url,
    readable_title,
    truncate_utf8,
)
from .ws import WsConn
from .rpc import HostClient, exchange_cookie
from .runtime import (
    RUNTIME_MARKER_FILENAME,
    RuntimeProcess,
    clear_runtime_record,
    reap_stale_runtime,
    runtime_marker_path,
    runtime_record_path,
    write_runtime_marker,
    write_runtime_record,
)
from .driver import TurnDriver
from .backend import WebBackend

__all__ = [
    "RuntimeProcess",
    "AuthError",
    "CancellationUnconfirmed",
    "HostClient",
    "ProtocolError",
    "StartupError",
    "TransportError",
    "TurnDriver",
    "WebBackend",
    "WebBackendError",
    "WsConn",
    "clear_runtime_record",
    "exchange_cookie",
    "reap_stale_runtime",
    "runtime_marker_path",
    "runtime_record_path",
    "seed_settings",
    "write_runtime_marker",
    "write_runtime_record",
    "readable_title",
    "identity_line",
    "clean_title_text",
    "truncate_utf8",
    "parse_ui_url",
    "parse_runtime_url",
    "TITLE_LIMIT_BYTES",
    "IDENTITY_LIMIT_BYTES",
    "IDENTITY_PREFIX",
    "DETAIL_LIMIT",
    "WS_FRAME_MAX_BYTES",
    "WS_HANDSHAKE_TIMEOUT",
    "RUNTIME_MARKER_FILENAME",
]

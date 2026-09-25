"""URL parsing, text normalization, and small constants.

Shared normalization, private file output and access to the installed YAML parser.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

# --- Public constants (test overrides). --------------------------------------

TITLE_LIMIT_BYTES = 80  # dsh-session-title maxTitleBytes
IDENTITY_LIMIT_BYTES = 40  # dsh-session-title fallbackMaxBytes
IDENTITY_PREFIX = "Session "
DETAIL_LIMIT = 200
WS_FRAME_MAX_BYTES = 1 << 20  # 1 MiB safety cap on incoming WS frame payload
WS_HANDSHAKE_TIMEOUT = 10.0
DEFAULT_CONFIRM_WINDOW = 8.0  # seconds to wait for turn/end after cancel


def _yaml_bridge(text: str, operation: str) -> str:
    """Use the installed DSH parser; private input travels only over stdin."""
    import shutil

    dsh = shutil.which("dsh")
    anchor = str(Path(dsh).resolve()) if dsh else str(Path.cwd() / "package.json")
    program = """const fs = require('node:fs');
const {createRequire} = require('node:module');
const local = createRequire(process.argv[1]);
let yaml;
try { yaml = local('js-yaml'); } catch { yaml = require('js-yaml'); }
try {
 const input = fs.readFileSync(0, 'utf8');
 const value = process.argv[2] === 'load'
   ? JSON.stringify(yaml.load(input) ?? {}) : yaml.dump(JSON.parse(input));
 process.stdout.write(value);
} catch { process.stderr.write('invalid YAML or JSON'); process.exitCode = 1; }
"""
    try:
        result = subprocess.run(
            ["node", "-e", program, anchor, operation],
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("DSH YAML parser unavailable") from None
    if result.returncode:
        raise ValueError(
            "DSH YAML parser failed; check settings syntax and DSH installation"
        )
    return result.stdout


def _yaml_safe_load(text: str) -> Any:
    import json

    return json.loads(_yaml_bridge(text, "load"))


def _yaml_safe_dump(value: Any) -> str:
    import json

    return _yaml_bridge(json.dumps(value, ensure_ascii=False), "dump")


# --- Text normalization (mirror dsh-session-title). ---------------------------


_OSC_SEQUENCE = re.compile(
    r"(?:\u001B\]|\u009D)(?:(?!\u0007|\u001B\\)[\s\S])*(?:\u0007|\u001B\\|$)"
)
_CSI_SEQUENCE = re.compile(r"\u001B\[[0-?]*[ -/]*[@-~]|\u009B[0-?]*[ -/]*[@-~]")
_ESC_SEQUENCE = re.compile(r"\u001B[@-_]")
_CONTROL = re.compile(r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]")
_DIRECTIONAL = re.compile(
    r"[\u200B\u200E\u200F\u202A-\u202E\u2060-\u2064\u2066-\u206F\uFEFF]"
)


def clean_title_text(value: str) -> str:
    value = _OSC_SEQUENCE.sub("", value)
    value = _CSI_SEQUENCE.sub("", value)
    value = _ESC_SEQUENCE.sub("", value)
    value = _CONTROL.sub("", value)
    value = _DIRECTIONAL.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def truncate_utf8(value: str, max_bytes: int) -> str:
    if len(value.encode("utf-8")) <= max_bytes:
        return value
    used = 0
    out: list[str] = []
    for character in value:
        size = len(character.encode("utf-8"))
        if used + size > max_bytes:
            break
        out.append(character)
        used += size
    return "".join(out)


def readable_title(session_key: str, max_bytes: int = TITLE_LIMIT_BYTES) -> str:
    return truncate_utf8(clean_title_text(session_key), max_bytes).strip()


def identity_line(session_key: str, max_bytes: int = IDENTITY_LIMIT_BYTES) -> str:
    body = clean_title_text(session_key)
    line = truncate_utf8(f"{IDENTITY_PREFIX}{body}", max_bytes)
    return line.strip() or "Session scout"


# --- Output helpers (used across modules). -----------------------------------


def clip(value: Any, limit: int = DETAIL_LIMIT) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


def scrub_token(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***")
    return clip(text)


# --- URL parsing. -------------------------------------------------------------


def parse_ui_url(raw: str) -> tuple[str, str | None]:
    """Split a configured UI URL into ``(origin, token)``."""
    value = raw.strip()
    if not value:
        raise ValueError("UI URL is empty")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("UI URL must be an http(s) origin")
    if parsed.username or parsed.password:
        raise ValueError("UI URL must not embed credentials")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    token = _query_token(parsed.query)
    return origin, token


def parse_runtime_url(raw: str) -> tuple[str, str, str | None]:
    """Parse the runtime's own ``dsh web: <url>`` announcement.

    Returns ``(origin, path, token)``.
    """
    line = raw.strip()
    parsed = urllib.parse.urlsplit(line)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("runtime URL missing scheme/netloc")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path or "/"
    token = _query_token(parsed.query)
    return origin, path, token


def _query_token(query: str) -> str | None:
    token = None
    for key, values in urllib.parse.parse_qs(query).items():
        if key == "token" and values and values[0]:
            token = values[0]
    return token


# --- Safe file creation. -----------------------------------------------------


def atomic_private_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Write ``content`` to ``path`` so the file is never world-readable.

    Uses a sibling temp file + ``os.rename`` so a partial file never appears
    in place. The directory permissions are tightened if the file already
    existed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid_str()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def uuid_str() -> str:
    import uuid

    return uuid.uuid4().hex

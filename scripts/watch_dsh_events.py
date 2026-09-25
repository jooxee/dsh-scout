#!/usr/bin/env python3
"""Cursor watcher for DSH scout supervision events.

Waits for the first lifecycle event of one controller mode and session key with
a sequence strictly greater than the requested cursor, then prints exactly one
JSON object to stdout and exits successfully. On timeout it exits with code 42
and fabricates no event.

Delivery is at least once; subscribers deduplicate by ``event_id``. A corrupt
or truncated final JSONL record is ignored until the writer completes it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any


CURSOR_DEFAULT = 0
POLL_INTERVAL_SECONDS = 0.2
TIMEOUT_EXIT = 42
TERMINAL_EVENT_KINDS = frozenset({"action_required", "turn_completed", "turn_failed"})


def default_state_root() -> Path:
    """Default root is the same controller state root the launcher uses."""
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "state/dsh-scout"


def event_file(state_root: Path, mode: str) -> Path:
    """Resolve the event stream under the controller-owned state root.

    Session keys are data fields, never path components.
    """
    return state_root / "events" / f"{mode}.events.jsonl"


def read_events(path: Path) -> list[dict[str, Any]]:
    """Parse the event stream, tolerating a corrupt or truncated final record."""
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def matches(
    event: dict[str, Any],
    *,
    mode: str,
    session_key: str,
    after_sequence: int,
    terminal_only: bool,
) -> bool:
    sequence = event.get("sequence")
    return (
        event.get("mode") == mode
        and event.get("session_key") == session_key
        and isinstance(sequence, int)
        and sequence > after_sequence
        and (not terminal_only or event.get("kind") in TERMINAL_EVENT_KINDS)
    )


def find_event(
    events: list[dict[str, Any]],
    *,
    mode: str,
    session_key: str,
    after_sequence: int,
    terminal_only: bool,
) -> dict[str, Any] | None:
    for event in events:
        if matches(
            event,
            mode=mode,
            session_key=session_key,
            after_sequence=after_sequence,
            terminal_only=terminal_only,
        ):
            return event
    return None


def wait_for_event(path: Path, deadline: float, listener: dict[str, Any]) -> dict[str, Any] | None:
    """Poll the stream until one matching event appears or time runs out."""
    while time.monotonic() < deadline:
        event = find_event(read_events(path), **listener)
        if event is not None:
            return event
        time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
    return find_event(read_events(path), **listener)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "watch DSH events")
    parser.add_argument("--mode", choices=["write", "read"], required=True)
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--after-sequence", type=int, default=CURSOR_DEFAULT)
    parser.add_argument("--timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--terminal-only", action="store_true")
    parser.add_argument(
        "--state-root",
        type=Path,
        default=None,
        help="custom/test-only controller state root; defaults to $CODEX_HOME/state/dsh-scout",
    )
    args = parser.parse_args(argv)
    if args.after_sequence < 0:
        parser.error("--after-sequence must be non-negative")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if not args.session_key.strip():
        parser.error("--session-key must be a non-empty string")
    return args


def main() -> int:
    args = parse_args()
    state_root = args.state_root.resolve() if args.state_root is not None else default_state_root()
    path = event_file(state_root, args.mode)
    deadline = time.monotonic() + args.timeout_seconds
    event = wait_for_event(
        path,
        deadline,
        {
            "mode": args.mode,
            "session_key": args.session_key,
            "after_sequence": args.after_sequence,
            "terminal_only": args.terminal_only,
        },
    )
    if event is None:
        print(
            f"No DSH {args.mode} supervision event for session={args.session_key} within "
            f"{args.timeout_seconds:g}s after sequence {args.after_sequence}.",
            file=sys.stderr,
        )
        return TIMEOUT_EXIT
    print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

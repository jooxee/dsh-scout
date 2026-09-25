"""Observe the owning DSH process before dispatch, with bounded cancellation."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable

from .errors import CancellationUnconfirmed, ProtocolError, TransportError
from .protocol import readable_title
from .ws import WsConn


class Follow:
    """One bounded reader; the caller remains responsive during partial frames."""

    def __init__(self, client, session_id: str, deadline: float | None) -> None:
        parsed = urllib.parse.urlsplit(client.origin)
        self.conn = WsConn(
            parsed.hostname,
            parsed.port,
            "/api/remote.mux",
            client.cookie_header(),
            client.origin,
        )
        self.stream_id = uuid.uuid4().hex
        self.items: queue.Queue = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self.conn.send_text(
            json.dumps(
                {
                    "type": "open",
                    "streamId": self.stream_id,
                    "endpoint": "session/follow",
                    "payload": {
                        "args": {
                            "request": {
                                "address": {"kind": "session", "sessionId": session_id},
                                # The driver only needs the opening cursor/cwd;
                                # DSH otherwise sends 50 messages of history.
                                "maxMessages": 1,
                            }
                        }
                    },
                }
            )
        )
        self.reader = threading.Thread(target=self._read, args=(deadline,), daemon=True)
        self.reader.start()

    def _read(self, deadline: float | None) -> None:
        try:
            while not self.closed.is_set():
                text = self.conn.read_message(deadline=deadline)
                if text is None:
                    raise TransportError("follow connection closed")
                self._put(json.loads(text))
        except Exception as error:
            self._put(error)

    def _put(self, item: Any) -> None:
        while not self.closed.is_set():
            try:
                self.items.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def next(
        self, deadline: float | None, cancelled: Callable[[], bool] | None = None
    ) -> dict:
        while deadline is None or time.monotonic() < deadline:
            if cancelled and cancelled():
                raise TimeoutError("requester disconnected")
            try:
                wait = 0.1 if deadline is None else min(
                    0.1, max(0.001, deadline - time.monotonic())
                )
                item = self.items.get(timeout=wait)
            except queue.Empty:
                continue
            return self._value(item)
        raise TimeoutError("DSH turn deadline exceeded")

    def _value(self, item: Any) -> dict:
        if isinstance(item, Exception):
            if isinstance(item, TransportError):
                # Reader errors are locally generated bounded diagnostics,
                # never transcript content or authenticated URLs.
                raise TransportError(f"follow reader failed: {item}") from item
            raise TransportError("follow reader failed") from item
        if not isinstance(item, dict) or item.get("streamId") != self.stream_id:
            raise ProtocolError("invalid follow stream identity")
        if item.get("type") != "item" or not isinstance(item.get("value"), dict):
            raise ProtocolError("follow ended or returned an error")
        return item["value"]

    def close(self) -> None:
        self.closed.set()
        self.conn.close()
        self.reader.join(timeout=1)


class TurnDriver:
    def __init__(
        self,
        *,
        runtime,
        client,
        session_key: str,
        cwd,
        confirm_window: float = 8,
        confirm_unconfirmed=None,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.cwd = os.fspath(cwd)
        self.title = readable_title(session_key)
        self.confirm_window = confirm_window
        self.confirm_unconfirmed = confirm_unconfirmed
        self.session_id = None
        self.workspace_id = None

    def ensure_session(self, *, session_id: str) -> str:
        workspace = self.client.call(
            "workspace/create", {"request": {"path": self.cwd}}
        )
        self.workspace_id = workspace.get("workspace", {}).get("workspaceId")
        if not isinstance(self.workspace_id, str) or not self.workspace_id:
            raise ProtocolError("DSH workspace identity missing")
        self.client.call(
            "session/create",
            {"request": {"sessionId": session_id, "workspaceId": self.workspace_id}},
        )
        self.client.call(
            "session/rename",
            {"request": {"sessionId": session_id, "title": self.title}},
        )
        self.session_id = session_id
        return session_id

    def run_prompt(
        self,
        *,
        session_id: str,
        request_id: str,
        content: list,
        timeout: float | None = None,
        cancel_check=None,
    ) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        follow = Follow(
            self.client,
            session_id,
            None if deadline is None else deadline + self.confirm_window + 20,
        )
        state = TurnState()
        try:
            snapshot_deadline = time.monotonic() + 15
            if deadline is not None:
                snapshot_deadline = min(deadline, snapshot_deadline)
            self._snapshot(follow, snapshot_deadline, cancel_check)
            self.client.call(
                "session/prompt",
                {
                    "request": {
                        "requestId": request_id,
                        "sessionId": session_id,
                        "mode": "queue",
                        "content": content,
                    }
                },
            )
            return self._observe(follow, state, deadline, cancel_check)
        finally:
            follow.close()

    def _snapshot(self, follow: Follow, deadline: float, cancelled) -> None:
        value = follow.next(deadline, cancelled)
        if value.get("type") != "snapshot":
            raise ProtocolError("DSH follow did not begin with snapshot")
        if value.get("header", {}).get("cwd") != self.cwd:
            raise ProtocolError("DSH session cwd differs from execution cwd")

    def _observe(
        self, follow: Follow, state: TurnState, deadline: float | None, cancelled
    ) -> dict:
        try:
            while True:
                value = follow.next(deadline, cancelled)
                if state.apply(value):
                    return state.result()
        except TimeoutError:
            return self._cancel(follow, state)

    def _cancel(self, follow: Follow, state: TurnState) -> dict:
        try:
            result = self.client.call(
                "session/cancel", {"request": {"sessionId": self.session_id}}
            )
            if not isinstance(result, dict) or result.get("accepted") is not True:
                raise ProtocolError("DSH cancellation was not accepted")
            deadline = time.monotonic() + self.confirm_window
            while not state.apply(follow.next(deadline)):
                pass
            return {**state.result(), "confirmed_cancel": True}
        except Exception:
            if self.confirm_unconfirmed:
                self.confirm_unconfirmed()
            raise CancellationUnconfirmed(
                "DSH cancellation could not be confirmed"
            ) from None


class TurnState:
    def __init__(self) -> None:
        self.turn: int | None = None
        self.text = ""
        self.error: str | None = None
        self.end: dict | None = None

    def apply(self, value: dict) -> bool:
        if value.get("type") != "event":
            raise ProtocolError("unexpected follow item")
        event = value.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise ProtocolError("malformed DSH event")
        kind, data = event.get("type"), event["data"]
        if kind == "turn/start":
            self._start(data)
        elif self.turn is not None:
            self._event(kind, data, event)
        return self.end is not None

    def _start(self, data: dict) -> None:
        turn = data.get("turn")
        if self.turn is not None or not isinstance(turn, int):
            raise ProtocolError("unexpected concurrent DSH turn")
        self.turn = turn

    def _event(self, kind: str, data: dict, event: dict) -> None:
        if data.get("turn", self.turn) != self.turn:
            raise ProtocolError("DSH event belongs to another turn")
        if kind == "assistant/message":
            texts = [
                b.get("text", "")
                for b in data.get("message", {}).get("content", [])
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if texts:
                self.text = "".join(texts)
        elif kind in {"turn/error", "agent/error"}:
            self.error = "DSH provider reported a turn failure"
        elif kind == "turn/end":
            self.end = event
            reason = data.get("reason", {})
            if reason.get("kind") != "completed":
                self.error = "DSH turn ended: " + str(reason.get("kind", "unknown"))

    def result(self) -> dict:
        return {
            "answer": self.text.strip(),
            "error": self.error,
            "completed": True,
            "saw_start": self.turn is not None,
            "turn": self.turn,
            "turn_end": self.end,
        }

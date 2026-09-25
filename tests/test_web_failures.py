"""Regression checks against real sockets and the public controller boundary."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_web_backend import controller
from dsh_web.driver import TurnState, TurnDriver
from dsh_web.errors import ProtocolError, CancellationUnconfirmed, StartupError
from dsh_web.runtime import RuntimeProcess, reap_stale_runtime, read_runtime_record
from dsh_web.ws import WsConn


def event(kind, **data):
    return {"type": "event", "event": {"type": kind, "data": {"turn": 1, **data}}}


class TurnFailureTests(unittest.TestCase):
    def test_end_reason_without_separate_error_is_failure(self):
        for reason in ("error", "blocked", "aborted", "max-tokens", "interrupted"):
            with self.subTest(reason=reason):
                state = TurnState()
                state.apply(event("turn/start"))
                self.assertTrue(state.apply(event("turn/end", reason={"kind": reason})))
                self.assertTrue(state.result()["error"])

    def test_other_turn_is_not_accepted(self):
        state = TurnState()
        state.apply(event("turn/start"))
        with self.assertRaises(ProtocolError):
            state.apply(event("turn/end", turn=2, reason={"kind": "completed"}))

    def test_cancel_requires_terminal_event(self):
        for accepted in (False, True):
            client = mock.Mock()
            client.call.return_value = {"accepted": accepted}
            terminate = mock.Mock()
            driver = TurnDriver(
                runtime=None,
                client=client,
                session_key="test",
                cwd="/tmp",
                confirm_window=0.01,
                confirm_unconfirmed=terminate,
            )
            follow = mock.Mock()
            follow.next.side_effect = TimeoutError()
            with self.assertRaises(CancellationUnconfirmed):
                driver._cancel(follow, TurnState())
            terminate.assert_called_once()

    def test_confirmed_cancel_still_reports_cancellation(self):
        client = mock.Mock()
        client.call.return_value = {"accepted": True}
        driver = TurnDriver(runtime=None, client=client, session_key="test", cwd="/tmp")
        follow = mock.Mock()
        follow.next.side_effect = [
            event("turn/start"),
            event("turn/end", reason={"kind": "aborted"}),
        ]
        self.assertTrue(driver._cancel(follow, TurnState())["confirmed_cancel"])


class SocketRegressionTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.ws = object.__new__(WsConn)
        self.ws.sock = self.left
        self.ws.buffer = b""
        self.ws.closed = False
        self.ws.send_lock = threading.Lock()
        self.ws.frame_max_bytes = 1024

    def tearDown(self):
        self.left.close()
        self.right.close()

    def test_controller_detects_disconnect(self):
        self.assertFalse(controller._client_disconnected(self.left))
        self.right.close()
        self.assertTrue(controller._client_disconnected(self.left))

    def test_fragmented_utf8_message_with_ping(self):
        self.right.sendall(b"\x01\x01\xc3\x89\x01x\x80\x01\xa9")
        self.assertEqual(self.ws.read_message(time.monotonic() + 1), "é")

    def test_partial_ping_obeys_deadline(self):
        self.right.sendall(b"\x89\x05x")
        started = time.monotonic()
        with self.assertRaises(Exception):
            self.ws.read_message(time.monotonic() + 0.1)
        self.assertLess(time.monotonic() - started, 1)

    def test_handshake_tail_not_required_to_make_socket_readable(self):
        self.ws.buffer = b"\x81\x02OK"
        self.assertEqual(self.ws.read_message(time.monotonic() + 1), "OK")


class RuntimeRecoveryTests(unittest.TestCase):
    def test_partial_startup_line_times_out_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def factory(argv, cwd, env):
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import time; print('partial', end='', flush=True); time.sleep(30)",
                    ],
                    stdout=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )

            runtime = RuntimeProcess(
                state_dir=root,
                mode="write",
                cwd=root,
                user_home=root,
                provider="p",
                model="m",
                startup_timeout=0.15,
                command_factory=factory,
            )
            with mock.patch("dsh_web.runtime._resolve_dsh", return_value="test-dsh"):
                with self.assertRaises(StartupError):
                    runtime.start()
            self.assertIsNone(runtime.process)
            self.assertIsNone(read_runtime_record(root, "write"))

    def test_stale_reaper_rejects_reused_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            from dsh_web.runtime import write_runtime_marker, write_runtime_record

            write_runtime_marker(root, "write")
            write_runtime_record(
                root,
                "write",
                pid=os.getpid(),
                generation=1,
                patch_path=root / "patch",
                settings_path=root / "settings",
                owner_uid=os.getuid(),
            )
            with mock.patch("os.killpg") as kill:
                self.assertFalse(reap_stale_runtime(root, "write", os.getuid()))
                kill.assert_not_called()

    def test_stale_reaper_checks_start_ticks_then_stops_owned_group(self):
        from dsh_web.runtime import (
            write_runtime_marker,
            write_runtime_record,
            runtime_record_path,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            patch = root / "patch"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                    "--patch",
                    str(patch),
                ],
                start_new_session=True,
            )
            try:
                write_runtime_marker(root, "write")
                write_runtime_record(
                    root,
                    "write",
                    pid=proc.pid,
                    generation=1,
                    patch_path=patch,
                    settings_path=root / "settings",
                    owner_uid=os.getuid(),
                )
                record = read_runtime_record(root, "write")
                path = runtime_record_path(root, "write")
                path.write_text(json.dumps({**record, "start_ticks": "wrong"}))
                self.assertFalse(reap_stale_runtime(root, "write", os.getuid()))
                self.assertIsNone(proc.poll())
                path.write_text(json.dumps(record))
                self.assertTrue(reap_stale_runtime(root, "write", os.getuid()))
                self.assertIsNotNone(proc.poll())
                self.assertIsNone(read_runtime_record(root, "write"))
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()


class ControllerAdmissionTests(unittest.TestCase):
    def test_prompt_admission_rejects_a_second_client(self):
        daemon = object.__new__(controller.SessionDaemon)
        daemon.stopping = False
        daemon._admission_lock = threading.Lock()
        daemon._admission_lock.acquire()
        response = daemon._handle_prompt({}, None)
        self.assertFalse(response["ok"])
        self.assertTrue(daemon._admission_lock.locked())

    def test_startup_failure_emits_terminal_failure_without_sdk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daemon = controller.SessionDaemon(
                "write", root, root / "sock", root / "state", backend="web"
            )
            with mock.patch.object(
                daemon, "_ensure_web", side_effect=StartupError("port unavailable")
            ):
                with self.assertRaises(StartupError):
                    daemon.handle(
                        {
                            "action": "prompt",
                            "session_key": "test",
                            "prompt": "hello",
                            "timeout": 1,
                        }
                    )
            events = [
                json.loads(line)
                for line in (root / "events/write.events.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(
                [e["kind"] for e in events], ["turn_started", "turn_failed"]
            )
            self.assertIsNone(daemon.sdk)

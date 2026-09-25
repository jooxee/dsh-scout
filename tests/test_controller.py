import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts/run_dsh_session.py"
SPEC = importlib.util.spec_from_file_location("run_dsh_session", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ControllerTests(unittest.TestCase):
    def test_sdk_reader_waits_for_a_late_frame_without_turn_deadline(self) -> None:
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "r")
        writer = os.fdopen(write_fd, "w")
        sdk = object.__new__(controller.DshSdk)
        sdk.process = SimpleNamespace(stdout=reader, poll=lambda: None)

        def deliver() -> None:
            time.sleep(0.15)
            writer.write(json.dumps({"method": "session.status"}) + "\n")
            writer.flush()

        sender = threading.Thread(target=deliver)
        sender.start()
        try:
            self.assertEqual(sdk._read_frame(None)["method"], "session.status")
        finally:
            sender.join(timeout=1)
            reader.close()
            writer.close()

    def test_prompt_defaults_to_no_turn_deadline(self) -> None:
        valid, error = controller._validate_prompt(
            {"session_key": "repo:issue-14:writer", "prompt": "Work until done"}
        )
        self.assertIsNone(error)
        self.assertEqual(valid, ("repo:issue-14:writer", "Work until done", None))

    def test_legacy_turn_timeout_is_rejected_before_dispatch(self) -> None:
        valid, error = controller._validate_prompt(
            {
                "session_key": "repo:issue-14:writer",
                "prompt": "Work until done",
                "timeout": 3600,
            }
        )
        self.assertIsNone(valid)
        self.assertIn("no time limit", error["error"])

    def test_public_launcher_rejects_turn_timeout_option(self) -> None:
        parser = controller._build_argument_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["--mode", "write", "--cwd", "/tmp", "--timeout-seconds", "3600"]
            )

    def test_launcher_waits_without_socket_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.txt"
            prompt.write_text("Keep working")
            args = SimpleNamespace(
                prompt_file=prompt,
                runtime_dir=root / "runtime",
                state_file=root / "state.json",
                socket=root / "controller.sock",
                session_key="repo:issue-14:writer",
                rotate=False,
                new_session=False,
            )
            with (
                mock.patch.object(controller, "ensure_daemon"),
                mock.patch.object(controller, "exchange", return_value={"ok": True}) as exchange,
                mock.patch.object(controller, "_emit_prompt_response_log"),
            ):
                self.assertEqual(controller._client_dispatch_prompt(args), 0)
            self.assertIsNone(exchange.call_args.args[2])
            self.assertNotIn("timeout", exchange.call_args.args[1])

    def test_session_ids_are_unique_and_namespaced(self) -> None:
        first = controller.session_id()
        second = controller.session_id()
        self.assertTrue(first.startswith("session-dsh-scout-"))
        self.assertNotEqual(first, second)

    def test_atomic_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            controller.atomic_json(path, {"turns": 2})
            self.assertEqual(controller.read_json(path), {"turns": 2})

    def test_reader_command_mounts_configured_roots_and_dev(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cwd = root / "project"
            cwd.mkdir()
            args = SimpleNamespace(
                mode="read",
                cwd=cwd,
                socket=root / "reader.sock",
                state_file=root / "reader.json",
                backend="web",
            )
            with (
                mock.patch.object(controller.shutil, "which", return_value="/usr/bin/bwrap"),
                mock.patch.dict(os.environ, {"DSH_SCOUT_READONLY_ROOTS": str(root)}, clear=False),
            ):
                command = controller.daemon_command(args, MODULE_PATH)
            self.assertEqual(command[0], "/usr/bin/bwrap")
            self.assertIn("--dev-bind", command)
            self.assertIn("--ro-bind", command)
            self.assertIn(str(root.resolve()), command)
            self.assertIn(str(cwd.resolve()), command)

    def test_writer_command_does_not_add_outer_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(
                mode="write",
                cwd=root,
                socket=root / "writer.sock",
                state_file=root / "writer.json",
                backend="web",
            )
            command = controller.daemon_command(args, MODULE_PATH)
            self.assertEqual(command[0], os.sys.executable)
            self.assertNotIn("--ro-bind", command)

    def test_prompt_state_is_visible_while_sdk_turn_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "writer.json"
            daemon = object.__new__(controller.SessionDaemon)
            daemon.mode = "write"
            daemon.cwd = root
            daemon.socket_path = root / "writer.sock"
            daemon.state_path = state_path
            daemon.state_root = root
            daemon.handoff_root = root / "handoffs"
            daemon.stopping = False
            daemon.sessions = {}
            daemon.events = controller.SupervisionEvents(root / "events" / "write.events.jsonl", "write")

            class FakeSdk:
                def prompt(self, identifier, text, timeout):
                    state = controller.read_json(state_path)
                    active = state["sessions"]["repo:issue-2:writer"]
                    self.identifier = identifier
                    self.text = text
                    self.timeout = timeout
                    self.observed = active.copy()
                    return "finished"

            daemon.sdk = FakeSdk()
            with mock.patch.object(
                controller,
                "wait_session_stats",
                return_value={"context_tokens": 42, "context_window": 1000},
            ):
                response = daemon.handle(
                    {
                        "action": "prompt",
                        "session_key": "repo:issue-2:writer",
                        "prompt": "Do one bounded task.",
                    }
                )

            observed = daemon.sdk.observed
            self.assertEqual(observed["status"], "running")
            self.assertIsNone(daemon.sdk.timeout)
            self.assertEqual(observed["active_turn"], 1)
            self.assertEqual(observed["session_id"], daemon.sdk.identifier)
            self.assertIn("prompt_started_at", observed)
            saved = controller.read_json(state_path)["sessions"]["repo:issue-2:writer"]
            self.assertEqual(saved["status"], "idle")
            self.assertEqual(saved["turns"], 1)
            self.assertNotIn("active_turn", saved)
            self.assertEqual(response["text"], "finished")
            self.assertEqual(response["event"]["kind"], "turn_completed")
            self.assertEqual(response["action_required"], None)
            saved = controller.read_json(state_path)["sessions"]["repo:issue-2:writer"]
            self.assertEqual(saved["last_event"]["event_id"], response["event"]["event_id"])
            self.assertEqual(saved["last_event"]["kind"], "turn_completed")


if __name__ == "__main__":
    unittest.main()

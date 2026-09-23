import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts/run_dsh_session.py"
SPEC = importlib.util.spec_from_file_location("run_dsh_session", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


class ControllerTests(unittest.TestCase):
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
                        "timeout": 30,
                    }
                )

            observed = daemon.sdk.observed
            self.assertEqual(observed["status"], "running")
            self.assertEqual(observed["active_turn"], 1)
            self.assertEqual(observed["session_id"], daemon.sdk.identifier)
            self.assertIn("prompt_started_at", observed)
            saved = controller.read_json(state_path)["sessions"]["repo:issue-2:writer"]
            self.assertEqual(saved["status"], "idle")
            self.assertEqual(saved["turns"], 1)
            self.assertNotIn("active_turn", saved)
            self.assertEqual(response["text"], "finished")


if __name__ == "__main__":
    unittest.main()

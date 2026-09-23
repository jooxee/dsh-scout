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


if __name__ == "__main__":
    unittest.main()

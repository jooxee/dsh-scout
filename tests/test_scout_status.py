"""The cheap status path observes state and process identity only."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts/scout_status.py"
SPEC = importlib.util.spec_from_file_location("scout_status", SCRIPT)
assert SPEC and SPEC.loader
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)
KEY = "repo:issue-10:reader"


class ScoutStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        self.path = self.root / "read.json"
        self.state = {
            "mode": "read", "backend": "web", "cwd": "/repo", "pid": 123,
            "web": {"pid": 456, "origin": "http://127.0.0.1:1234/secret"},
            "provider": "approved-provider", "model": "approved-model",
            "sessions": {
                KEY: {
                    "status": "running", "active_turn": 2, "prompt_started_at": 100,
                    "session_id": "session-abc", "last_event": {
                        "kind": "turn_started", "sequence": 8,
                    },
                    "prompt": "private prompt", "answer": "private answer",
                },
                "another-key": {"status": "idle", "answer": "other private answer"},
            },
        }
        self.proc_command(123, [
            "python3", "/skill/run_dsh_session.py", "--serve", "--mode", "read",
            "--cwd", "/repo", "--state-file", str(self.path),
        ])
        self.proc_command(456, [
            "node", "/bin/dsh", "--patch", str(self.root / "read/web-patch.yml"),
            "--profile", "web",
        ])

    def proc_command(self, pid, args):
        directory = self.proc / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")

    def snapshot(self, details=False):
        return status.snapshot(
            self.state, mode="read", session_key=KEY, state_path=self.path,
            proc_root=self.proc, details=details, now=130,
        )

    def test_active_status_is_compact_and_does_not_contain_content_or_url(self):
        result = self.snapshot()
        self.assertEqual(result["phase"], "running")
        self.assertTrue(result["active"])
        self.assertEqual((result["controller"], result["runtime"]), ("alive", "alive"))
        self.assertEqual((result["turn"], result["event_sequence"]), (2, 8))
        encoded = json.dumps(result)
        for private in ("private", "secret", "another-key", "session-abc", "approved-model"):
            self.assertNotIn(private, encoded)
        self.assertLess(len(encoded), 300)

    def test_opt_in_details_are_bounded_and_content_free(self):
        result = self.snapshot(details=True)
        self.assertEqual(result["details"]["elapsed_seconds"], 30)
        encoded = json.dumps(result)
        for private in ("private", "secret", "another-key", "session-abc", "approved-model"):
            self.assertNotIn(private, encoded)
        self.assertLess(len(encoded), 700)

    def test_dead_or_reused_process_cannot_be_active(self):
        (self.proc / "123/cmdline").unlink()
        dead = self.snapshot()
        self.assertEqual((dead["phase"], dead["controller"], dead["active"]),
                         ("stale", "dead", False))
        self.proc_command(123, ["python3", "/other/run_dsh_session.py", "--serve",
                                "--mode", "read", "--cwd", "/other",
                                "--state-file", str(self.path)])
        mismatch = self.snapshot()
        self.assertEqual((mismatch["phase"], mismatch["controller"]),
                         ("stale", "mismatch"))
        self.proc_command(123, ["python3", "/skill/run_dsh_session.py", "--serve",
                                "--mode", "read", "--cwd", "/repo",
                                "--state-file", str(self.path)])
        self.proc_command(456, ["node", "/bin/dsh", "--patch", "/wrong/patch",
                                "--profile", "web"])
        self.assertEqual(self.snapshot()["runtime"], "mismatch")
        self.assertEqual(self.snapshot()["phase"], "stale")

    def test_terminal_phases_survive_controller_exit(self):
        (self.proc / "123/cmdline").unlink()
        record = self.state["sessions"][KEY]
        for kind, expected in (("turn_completed", "completed"),
                               ("action_required", "action_required"),
                               ("turn_failed", "failed")):
            with self.subTest(kind=kind):
                record["status"] = "error" if kind == "turn_failed" else "idle"
                record["last_event"] = {"kind": kind, "sequence": 9}
                actual = self.snapshot()
                self.assertEqual(actual["phase"], expected)
                self.assertFalse(actual["active"])
                self.assertEqual(actual["controller"], "dead")

    def test_missing_lost_and_malformed_state(self):
        self.assertEqual(status.snapshot(None, mode="read", session_key=KEY,
                                         state_path=self.path)["phase"], "missing")
        self.assertEqual(status.snapshot({"mode": "read", "sessions": {}},
                                         mode="read", session_key=KEY,
                                         state_path=self.path)["phase"], "missing")
        record = self.state["sessions"][KEY]
        record.clear()
        record["restarted"] = True
        self.assertEqual(self.snapshot()["phase"], "context_lost")
        record["last_event"] = {"kind": [], "sequence": "bad"}
        self.assertIsNone(self.snapshot(details=True)["event_sequence"])
        record["status"] = "idle"
        self.assertEqual(self.snapshot()["phase"], "idle")
        self.assertEqual(status.snapshot({}, mode="read", session_key=KEY,
                                         state_path=self.path)["phase"], "unknown")

    def test_cli_missing_state_does_not_create_files(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--mode", "read", "--session-key", KEY,
             "--state-root", str(self.root / "absent")],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"], "missing")
        self.assertFalse((self.root / "absent").exists())


if __name__ == "__main__":
    unittest.main()

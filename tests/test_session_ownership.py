"""Session continuity and process ownership, with no provider requests."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_supervision_events import SESSION_KEY, controller, make_daemon, run_prompt


class SessionOwnershipTests(unittest.TestCase):
    def test_xdg_cannot_split_default_socket_or_lock_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = []
            for xdg in (str(root / "desktop"), str(root / "shell")):
                args = SimpleNamespace(
                    mode="read", socket=None, state_file=root / "read.json"
                )
                with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": xdg}):
                    controller._apply_default_paths(args)
                results.append((args.socket, args.runtime_dir))
            self.assertEqual(results[0], results[1])

    def test_owner_lease_blocks_other_process_before_any_state_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "read.json"
            path.write_text('{"sentinel": true}')
            code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import run_dsh_session as c
with c.daemon_ownership(Path(sys.argv[2])):
    Path(sys.argv[2]).write_text('overwritten')
"""
            with controller.daemon_ownership(path):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(controller._SCRIPT_DIR),
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("owner", result.stderr.lower())
            self.assertEqual(json.loads(path.read_text()), {"sentinel": True})
            with controller.daemon_ownership(path):
                pass  # Releasing the previous owner's lease permits admission.

    def test_daemon_entrypoint_locks_before_constructing_or_reaping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "read.json"
            args = SimpleNamespace(
                serve=True,
                runtime_dir=root / "runtime",
                state_file=state,
                socket=root / "different.sock",
                mode="read",
                cwd=root,
                backend="web",
            )
            with (
                controller.daemon_ownership(state),
                mock.patch.object(controller, "parse_args", return_value=args),
                mock.patch.object(controller, "SessionDaemon") as constructor,
            ):
                with self.assertRaisesRegex(RuntimeError, "owner"):
                    controller.main()
                constructor.assert_not_called()

    def test_live_legacy_owner_is_not_reaped_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "read.json"
            with subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            ) as proc:
                try:
                    before = json.dumps({"pid": proc.pid, "sessions": {"old": {}}})
                    path.write_text(before)
                    with (
                        self.assertRaisesRegex(RuntimeError, "owner"),
                        controller.daemon_ownership(path),
                    ):
                        self.fail("must not construct the daemon")
                    self.assertIsNone(proc.poll())
                    self.assertEqual(path.read_text(), before)
                finally:
                    proc.terminate()
                    proc.wait(timeout=3)

    def test_lost_context_rejects_before_dispatch_until_explicit_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = mock.Mock()
            sdk.prompt.return_value = "finished"
            daemon = make_daemon(Path(temporary), sdk)
            daemon.sessions[SESSION_KEY] = {
                "session_id": "old",
                "live_session_lost": True,
            }
            reply = run_prompt(daemon)
            self.assertFalse(reply["ok"])
            self.assertIn("--new-session", reply["error"])
            sdk.prompt.assert_not_called()
            self.assertFalse((Path(temporary) / "events/write.events.jsonl").exists())
            reply = run_prompt(daemon, new_session=True)
            self.assertTrue(reply["ok"])
            self.assertNotEqual(reply["session_id"], "old")
            self.assertTrue(reply["restarted"])

    def test_controller_restart_also_requires_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = make_daemon(Path(temporary), mock.Mock())
            daemon.sessions[SESSION_KEY] = {
                "previous_session_id": "old",
                "restarted": True,
            }
            self.assertFalse(run_prompt(daemon)["ok"])
            daemon.sdk.prompt.assert_not_called()

    def test_action_required_followup_retains_identity_and_rejects_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            sdk = mock.Mock()
            sdk.prompt.return_value = (
                "Need input\n<dsh-scout-action-required-v1>\n"
                '{"summary":"choice","questions":["Which?"]}\n</dsh-scout-action-required-v1>'
            )
            daemon = make_daemon(Path(temporary), sdk)
            first = run_prompt(daemon)
            self.assertEqual(first["event"]["kind"], "action_required")
            self.assertFalse(run_prompt(daemon, new_session=True)["ok"])
            sdk.prompt.return_value = "done"
            second = run_prompt(daemon)
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertEqual(sdk.prompt.call_count, 2)
            self.assertEqual(daemon.sessions[SESSION_KEY]["turns"], 2)

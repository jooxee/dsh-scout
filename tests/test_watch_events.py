"""Tests for the standalone supervision event cursor watcher."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts/watch_dsh_events.py"
SPEC = importlib.util.spec_from_file_location("watch_dsh_events", SCRIPT)
assert SPEC and SPEC.loader
watcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watcher)

SESSION_KEY = "repo:issue-4:writer"


def command(state_root: Path, *extra: str) -> list[str]:
    return ["python3", str(SCRIPT), "--state-root", str(state_root), *extra]


def seats(state_root: Path, events: list[dict]) -> None:
    path = watcher.event_file(state_root, "write")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")


def sample(sequence: int, kind: str = "turn_completed", key: str = SESSION_KEY, mode: str = "write") -> dict:
    return {
        "version": 1,
        "sequence": sequence,
        "event_id": f"write:{sequence}",
        "kind": kind,
        "mode": mode,
        "session_key": key,
        "session_id": "session-dsh-scout-x",
        "turn": sequence,
        "emitted_at": 1_790_290_000,
        "verification_state": "pending",
    }


class WatcherUnitTests(unittest.TestCase):
    def test_read_events_tolerates_truncated_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "write.events.jsonl"
            with path.open("w") as stream:
                stream.write(json.dumps(sample(1)) + "\n")
                stream.write('{"version":1,"sequence":2,"kind":"turn_comp')
            events = watcher.read_events(path)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["sequence"], 1)

    def test_matches_filters_mode_key_cursor_and_terminal(self) -> None:
        arguments = {"mode": "write", "session_key": SESSION_KEY, "after_sequence": 2, "terminal_only": False}
        self.assertTrue(watcher.matches(sample(3), **arguments))
        self.assertFalse(watcher.matches(sample(2), **arguments))
        self.assertFalse(watcher.matches(sample(3, key="other:writer"), **arguments))
        self.assertFalse(watcher.matches(sample(3, mode="read"), **arguments))
        # An event without an integer sequence is skipped, not fatal.
        broken = dict(sample(4))
        broken["sequence"] = None
        self.assertFalse(watcher.matches(broken, **arguments))
        terminal_arguments = dict(arguments, terminal_only=True)
        self.assertFalse(watcher.matches(sample(3, kind="turn_started"), **terminal_arguments))
        self.assertTrue(watcher.matches(sample(3, kind="turn_failed"), **terminal_arguments))

    def test_find_event_returns_first_match_in_sequence_order(self) -> None:
        events = [sample(1, kind="turn_started"), sample(3), sample(4)]
        found = watcher.find_event(
            events, mode="write", session_key=SESSION_KEY, after_sequence=0, terminal_only=False
        )
        self.assertEqual(found and found["sequence"], 1)


class WatcherIntegrationTests(unittest.TestCase):
    def run_watcher(self, state_root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command(state_root, *extra),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_waits_and_emits_exactly_one_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launched = subprocess.Popen(
                command(root, "--mode", "write", "--session-key", SESSION_KEY),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # The watcher is already blocked: publish the start record now and
            # the terminal record after another moment.
            seats(root, [sample(1, kind="turn_started")])
            seats(root, [sample(2), sample(3, kind="turn_started")])
            completed = launched.communicate(timeout=20)
        self.assertEqual(launched.returncode, 0)
        self.assertEqual(
            [line for line in completed[0].splitlines() if line.strip()],
            [json.dumps(sample(1, kind="turn_started"))],
        )

    def test_after_sequence_cursor_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seats(root, [sample(1), sample(2, kind="action_required")])
            completed = self.run_watcher(root, "--mode", "write", "--session-key", SESSION_KEY, "--after-sequence", "1")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), json.dumps(sample(2, kind="action_required")))

    def test_timeout_exits_with_documented_code_and_no_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed = self.run_watcher(
                Path(temporary), "--mode", "write", "--session-key", SESSION_KEY,
                "--timeout-seconds", "0.5", "--terminal-only",
            )
        self.assertEqual(completed.returncode, watcher.TIMEOUT_EXIT)
        self.assertEqual(completed.stdout, "")
        self.assertTrue(completed.stderr.strip())

    def test_terminal_only_skips_started_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seats(root, [sample(1, kind="turn_started"), sample(2), sample(3, kind="action_required")])
            completed = self.run_watcher(
                root,
                "--mode", "write", "--session-key", SESSION_KEY,
                "--after-sequence", "1", "--terminal-only",
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout.strip())["kind"], "turn_completed")

    def test_unknown_session_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seats(root, [sample(1)])
            completed = self.run_watcher(
                root, "--mode", "write", "--session-key", "repo:none:writer", "--timeout-seconds", "0.3"
            )
        self.assertEqual(completed.returncode, watcher.TIMEOUT_EXIT)

    def test_oversized_session_key_is_rejected_not_clipped(self) -> None:
        max_key = "r:" + "a" * (watcher.SESSION_KEY_LIMIT - 2)
        over_key = max_key + "b"
        self.assertEqual(len(over_key), watcher.SESSION_KEY_LIMIT + 1)
        with tempfile.TemporaryDirectory() as temporary:
            completed = self.run_watcher(Path(temporary), "--mode", "write", "--session-key", over_key)
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotEqual(completed.returncode, watcher.TIMEOUT_EXIT)
        self.assertEqual(completed.stdout, "")
        self.assertIn("at most 200", completed.stderr)

    def test_session_key_at_limit_is_watched(self) -> None:
        max_key = "r:" + "a" * (watcher.SESSION_KEY_LIMIT - 2)
        self.assertEqual(len(max_key), watcher.SESSION_KEY_LIMIT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seats(root, [sample(1, key=max_key)])
            completed = self.run_watcher(
                root, "--mode", "write", "--session-key", max_key, "--timeout-seconds", "2"
            )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout.strip())["session_key"], max_key)


if __name__ == "__main__":
    unittest.main()

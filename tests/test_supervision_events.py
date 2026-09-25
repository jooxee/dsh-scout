"""Tests for durable supervision events and the action-required contract."""

import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts/run_dsh_session.py"
SPEC = importlib.util.spec_from_file_location("run_dsh_session", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

SESSION_KEY = "repo:issue-4:writer"
EVENTS_FILE = "events/write.events.jsonl"


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        assert isinstance(value, dict)
        events.append(value)
    return events


def make_daemon(root: Path, sdk) -> controller.SessionDaemon:
    daemon = object.__new__(controller.SessionDaemon)
    daemon.mode = "write"
    daemon.cwd = root
    daemon.socket_path = root / "write.sock"
    daemon.state_path = root / "write.json"
    daemon.state_root = root
    daemon.handoff_root = root / "handoffs"
    daemon.stopping = False
    daemon.sessions = {}
    daemon.sdk = sdk
    daemon.events = controller.SupervisionEvents(root / "events" / "write.events.jsonl", "write")
    return daemon


def run_prompt(daemon, response_text: str | None = None, sdk=None, **request):
    with (
        mock.patch.object(controller, "repository_facts", return_value={"available": False}),
        mock.patch.object(
            controller,
            "wait_session_stats",
            return_value={"context_tokens": 10, "context_window": 1000},
        ),
    ):
        return daemon.handle(
            {
                "action": "prompt",
                "session_key": SESSION_KEY,
                "prompt": "Do one bounded task.",
                "timeout": 30,
                **request,
            }
        )


class SupervisionEventsTests(unittest.TestCase):
    def test_sequence_recovery_skips_truncated_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "write.events.jsonl"
            events = controller.SupervisionEvents(path, "write")
            first = events.append(kind="turn_started", session_key=SESSION_KEY, session_id_value="s1", turn=1)
            second = events.append(kind="turn_completed", session_key=SESSION_KEY, session_id_value="s1", turn=1)
            self.assertEqual([first["sequence"], second["sequence"]], [1, 2])
            with path.open("a") as stream:
                stream.write('{"version":1,"sequence":3,"kind":"turn_comp')
            resumed = controller.SupervisionEvents(path, "write")
            third = resumed.append(kind="turn_failed", session_key=SESSION_KEY, session_id_value="s1", turn=1)
            self.assertEqual(third["sequence"], 3)
            self.assertEqual(len(read_events(path)), 3)

    def test_event_files_are_user_private_with_bounded_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "write.events.jsonl"
            events = controller.SupervisionEvents(path, "write")
            event = events.append(kind="turn_started", session_key=SESSION_KEY, session_id_value="s1", turn=1)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(event["version"], 1)
            self.assertEqual(event["event_id"], "write:1")
            non_terminal_fields = sorted(event)
            self.assertEqual(
                non_terminal_fields,
                [
                    "emitted_at",
                    "event_id",
                    "kind",
                    "mode",
                    "sequence",
                    "session_id",
                    "session_key",
                    "turn",
                    "version",
                ],
            )

    def test_bad_kind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = controller.SupervisionEvents(Path(temporary) / "e.jsonl", "write")
            with self.assertRaises(ValueError):
                events.append(kind="text", session_key=SESSION_KEY, session_id_value="s1", turn=1)


class HandleEventOrderTests(unittest.TestCase):
    def make_daemon_with_spy_sdk(self, answer: str | Exception):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        daemon = make_daemon(root, None)
        core = self

        class SpySdk:
            state_at_prompt: dict = {}

            def prompt(self, identifier, text, timeout):
                if isinstance(answer, Exception):
                    raise answer
                self.identifier = identifier
                core.prompt_text = text
                self.state_at_prompt = {
                    "status": (controller.read_json(daemon.state_path)["sessions"][SESSION_KEY].get("status")),
                    "events": read_events(root / EVENTS_FILE),
                }
                return answer

        daemon.sdk = SpySdk()
        return root, daemon

    def test_start_then_completed_event_order_and_state(self) -> None:
        root, daemon = self.make_daemon_with_spy_sdk("done")
        response = run_prompt(daemon)
        self.assertEqual(response["event"]["kind"], "turn_completed")
        events = read_events(root / EVENTS_FILE)
        self.assertEqual([e["kind"] for e in events], ["turn_started", "turn_completed"])
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        # State persisted first, then turn_started: at prompt dispatch the
        # stream held exactly one event and the session status was running.
        self.assertEqual(daemon.sdk.state_at_prompt["events"], events[:1])
        self.assertEqual(daemon.sdk.state_at_prompt["status"], "running")
        completed = events[1]
        self.assertEqual(completed["verification_state"], "pending")
        self.assertNotIn("action", completed)
        self.assertEqual(completed["event_id"], "write:2")
        state = controller.read_json(daemon.state_path)["sessions"][SESSION_KEY]
        self.assertEqual(state["last_event"]["event_id"], completed["event_id"])
        self.assertEqual(state["last_event"]["kind"], "turn_completed")
        self.assertEqual(state["verification"]["state"], "pending")
        # The supervision preamble is part of every delegated prompt.
        self.assertIn("SUPERVISION PROTOCOL", self.prompt_text)
        self.assertIn(controller.ACTION_OPEN, self.prompt_text)

    def test_action_required_event_from_terminal_envelope(self) -> None:
        answer = (
            "I need a decision before code changes.\n"
            f"{controller.ACTION_OPEN}\n"
            '{"summary":"Material product choice","questions":["Ship A or B?"]}\n'
            f"{controller.ACTION_CLOSE}"
        )
        root, daemon = self.make_daemon_with_spy_sdk(answer)
        response = run_prompt(daemon)
        self.assertEqual(response["event"]["kind"], "action_required")
        events = read_events(root / EVENTS_FILE)
        self.assertEqual([e["kind"] for e in events], ["turn_started", "action_required"])
        action_event = events[1]
        self.assertEqual(action_event["verification_state"], "pending")
        self.assertEqual(
            action_event["action"],
            {"trust": "scout-declared", "summary": "Material product choice", "questions": ["Ship A or B?"]},
        )
        state = controller.read_json(daemon.state_path)["sessions"][SESSION_KEY]
        self.assertEqual(state["status"], "idle")
        self.assertEqual(state["verification"]["state"], "pending")
        self.assertEqual(response["action_required"]["trust"], "scout-declared")
        self.assertEqual(response["last_event"]["kind"], "action_required")

    def test_valid_envelope_stays_out_of_returned_text(self) -> None:
        answer = (
            "Code changes are done.\n"
            f"{controller.ACTION_OPEN}\n"
            '{"summary":"Blocked","questions":["Proceed?"]}\n'
            f"{controller.ACTION_CLOSE}"
        )
        root, daemon = self.make_daemon_with_spy_sdk(answer)
        response = run_prompt(daemon)
        # The exact cleaned text: everything before the envelope's own line.
        self.assertEqual(response["text"], "Code changes are done.")
        self.assertNotIn(controller.ACTION_OPEN, json.dumps(response["text"]))
        events = read_events(root / EVENTS_FILE)
        record = next(e for e in events if e["kind"] == "action_required")
        self.assertEqual(record["action"]["summary"], "Blocked")

    def test_nonterminal_envelope_text_is_returned_verbatim(self) -> None:
        answer = (
            "Answer.\n"
            f"{controller.ACTION_OPEN}\n"
            '{"summary":"no","questions":["keep"]}\n'
            f"{controller.ACTION_CLOSE}\n"
            "more normal text"
        )
        root, daemon = self.make_daemon_with_spy_sdk(answer)
        response = run_prompt(daemon)
        self.assertEqual(response["text"], answer)
        self.assertIsNone(response["action_required"])
        self.assertEqual(response["event"]["kind"], "turn_completed")


    def test_turn_failed_event_after_error_state(self) -> None:
        root, daemon = self.make_daemon_with_spy_sdk(RuntimeError("DSH prompt failed: boom"))
        with self.assertRaises(RuntimeError), mock.patch.object(
            controller, "repository_facts", return_value={"available": False}
        ):
            run_prompt(daemon)
        state = controller.read_json(daemon.state_path)["sessions"][SESSION_KEY]
        self.assertEqual(state["status"], "error")
        events = read_events(root / EVENTS_FILE)
        self.assertEqual([e["kind"] for e in events], ["turn_started", "turn_failed"])
        self.assertEqual(events[1]["reason"], "prompt-failed")
        self.assertEqual(events[1]["verification_state"], "pending")
        self.assertEqual(state["last_event"]["kind"], "turn_failed")

    def test_append_failure_fails_prompt_visibly(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        daemon = make_daemon(root, None)

        class NeverCalledSdk:
            def prompt(self, identifier, text, timeout):  # pragma: no cover
                raise AssertionError("prompt must not run when event append fails")

        daemon.sdk = NeverCalledSdk()

        class FailingEvents(controller.SupervisionEvents):
            def append(self, **kwargs):
                raise OSError("append unavailable")

        daemon.events = FailingEvents(daemon.events.path, "write")
        with self.assertRaises(RuntimeError) as caught:
            run_prompt(daemon)
        self.assertIn("supervision event append failed", str(caught.exception))
        state = controller.read_json(daemon.state_path)["sessions"][SESSION_KEY]
        self.assertEqual(state["status"], "error")
        self.assertIn("supervision event append failed", state["last_error"])
        self.assertLessEqual(len(state["last_error"]), controller.SUPERVISION_ERROR_LIMIT)
        self.assertEqual(read_events(root / EVENTS_FILE), [])


class SessionKeyBoundaryTests(unittest.TestCase):
    def test_session_key_limit_enforced_not_clipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daemon = make_daemon(root, None)

            class NeverSdk:
                def prompt(self, identifier, text, timeout):  # pragma: no cover
                    raise AssertionError("no prompt may run for an invalid key")

            daemon.sdk = NeverSdk()
            max_key = "r:" + "a" * (controller.SESSION_KEY_LIMIT - 2)
            self.assertEqual(len(max_key), controller.SESSION_KEY_LIMIT)
            over_key = max_key + "b"
            self.assertEqual(len(over_key), controller.SESSION_KEY_LIMIT + 1)
            rejected = daemon.handle({"action": "prompt", "session_key": over_key, "prompt": "work"})
            self.assertFalse(rejected["ok"])
            self.assertIn("at most 200", rejected["error"])
            self.assertNotIn(over_key, (root / EVENTS_FILE).read_text() if (root / EVENTS_FILE).exists() else "")

    def test_session_key_at_limit_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daemon = make_daemon(root, None)

            class TextSdk:
                def prompt(self, identifier, text, timeout):
                    return "done"

            daemon.sdk = TextSdk()
            max_key = "r:" + "a" * (controller.SESSION_KEY_LIMIT - 2)
            with mock.patch.object(controller, "repository_facts", return_value={"available": False}), mock.patch.object(
                controller, "wait_session_stats", return_value={"context_tokens": 10, "context_window": 1000}
            ):
                response = daemon.handle({"action": "prompt", "session_key": max_key, "prompt": "work", "timeout": 30})
            self.assertTrue(response["ok"])
            events = read_events(root / EVENTS_FILE)
            for event in events:
                self.assertEqual(event["session_key"], max_key)

class ParseActionRequiredTests(unittest.TestCase):
    def envelope(self, body: str, prefix: str = "Working on it.") -> str:
        return f"{prefix}\n{controller.ACTION_OPEN}\n{body}\n{controller.ACTION_CLOSE}"

    def test_valid_terminal_envelope_is_extracted_and_bounded(self) -> None:
        text = self.envelope('{"summary":"reason","questions":["q1","q2"]}')
        remaining, action = controller.parse_action_required(text)
        self.assertEqual(remaining, "Working on it.")
        self.assertIsNotNone(action)
        self.assertEqual(action["trust"], "scout-declared")
        self.assertEqual(action["summary"], "reason")
        self.assertEqual(action["questions"], ["q1", "q2"])

    def test_envelope_in_middle_of_text_stays_ordinary(self) -> None:
        text = self.envelope('{"summary":"r","questions":["q"]}') + "\nmore text after"
        remaining, action = controller.parse_action_required(text)
        self.assertIsNone(action)
        self.assertEqual(remaining, text)

    def test_malformed_or_extra_field_envelope_is_rejected(self) -> None:
        for body in (
            "{not json",
            '["not an object"]',
            '{"summary":"r","questions":["q"],"extra":1}',
            '{"summary":123,"questions":["q"]}',
            '{"summary":"r","questions":[]}',
            '{"summary":"r","questions":["  "]}',
        ):
            remaining, action = controller.parse_action_required(self.envelope(body))
            self.assertIsNone(action)
            self.assertEqual(remaining, self.envelope(body))

    def test_oversized_fields_are_clipped_not_rejected(self) -> None:
        body = json.dumps(
            {
                "summary": "s" * 700,
                "questions": [f"q{i}" * 300 for i in range(6)],
            }
        )
        remaining, action = controller.parse_action_required(self.envelope(body))
        self.assertIsNotNone(action)
        self.assertEqual(len(action["summary"]), controller.ACTION_SUMMARY_LIMIT)
        self.assertEqual(len(action["questions"]), controller.ACTION_QUESTIONS_LIMIT)
        for question in action["questions"]:
            self.assertLessEqual(len(question), controller.ACTION_QUESTION_LIMIT)

    def test_envelope_without_leading_text_is_accepted(self) -> None:
        text = f"{controller.ACTION_OPEN}\n" + '{"summary":"r","questions":["q"]}\n' + f"{controller.ACTION_CLOSE}"
        remaining, action = controller.parse_action_required(text)
        self.assertEqual(remaining, "")
        self.assertEqual(action["summary"], "r")

    def test_preamble_contains_action_contract(self) -> None:
        instruction = controller.supervision_instruction()
        self.assertIn(controller.ACTION_OPEN, instruction)
        self.assertIn(controller.ACTION_CLOSE, instruction)
        self.assertIn("NOT authorization", instruction)
        for mode in ("write", "read"):
            self.assertIn("MODE", controller.mode_instruction(mode))


class StartFunctionTests(unittest.TestCase):
    def test_mode_instruction_unchanged_shape(self) -> None:
        write_mode = controller.mode_instruction("write")
        read_mode = controller.mode_instruction("read")
        self.assertIn("MODE: WRITE", write_mode)
        self.assertIn("MODE: READ_ONLY", read_mode)


class RestartRecoveryTests(unittest.TestCase):
    def make_restarted_daemon(self, state_path: Path, cwd: Path) -> controller.SessionDaemon:
        with mock.patch.object(controller, "DshSdk", return_value=None):
            daemon = controller.SessionDaemon("write", cwd, state_path.parent / "write.sock", state_path)
        return daemon

    def test_restart_emits_one_recovery_failure_per_running_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "write.json"
            controller.atomic_json(
                state_path,
                {
                    "version": 1,
                    "sessions": {
                        SESSION_KEY: {"session_id": "s-old", "status": "running", "active_turn": 2},
                        "repo:issue-5:writer": {"session_id": "s-idle", "status": "idle", "turns": 3},
                    },
                },
            )
            daemon = self.make_restarted_daemon(state_path, root)
            self.assertTrue(daemon.sessions[SESSION_KEY]["restarted"])
            # Session keys are data fields; the stream stays one file per mode.
            events = read_events(root / EVENTS_FILE)
            self.assertEqual(len(events), 1)
            recovery = events[0]
            self.assertEqual(recovery["kind"], "turn_failed")
            self.assertEqual(recovery["reason"], "controller-restart-recovery")
            self.assertEqual(recovery["session_id"], "s-old")
            self.assertEqual(recovery["session_key"], SESSION_KEY)
            self.assertEqual(recovery["turn"], 2)
            self.assertEqual(recovery["sequence"], 1)
            self.assertEqual(recovery["verification_state"], "pending")

            # A later prompt continues the same sequence.
            class TextSdk:
                def prompt(self, identifier, text, timeout):
                    return "ok"

            daemon.sdk = TextSdk()
            with mock.patch.object(controller, "repository_facts", return_value={"available": False}), mock.patch.object(
                controller, "wait_session_stats", return_value={"context_tokens": 10, "context_window": 1000}
            ):
                response = daemon.handle(
                    {"action": "prompt", "session_key": "repo:issue-6:writer", "prompt": "work", "timeout": 30}
                )
            self.assertEqual(response["event"]["sequence"], 3)
            self.assertEqual(response["event"]["event_id"], "write:3")
            kinds = [e["kind"] for e in read_events(root / EVENTS_FILE)]
            self.assertEqual(kinds, ["turn_failed", "turn_started", "turn_completed"])

    def test_recovery_identity_key_mismatch_appends_new_recovery(self) -> None:
        # A pre-existing recovery for this key must NOT suppress a later,
        # different abandoned turn even though the session key is identical.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "write.json"
            events_path = root / EVENTS_FILE
            controller.atomic_json(
                state_path,
                {"version": 1, "sessions": {SESSION_KEY: {"session_id": "s-new", "status": "running", "active_turn": 4}}},
            )
            stream = controller.SupervisionEvents(events_path, "write")
            stream.append(
                kind="turn_failed",
                session_key=SESSION_KEY,
                session_id_value="s-old",
                turn=2,
                reason=controller.RECOVERY_REASON,
            )
            daemon = self.make_restarted_daemon(state_path, root)
            events = read_events(events_path)
            recoveries = [e for e in events if e.get("reason") == controller.RECOVERY_REASON]
            self.assertEqual(len(recoveries), 2)
            self.assertEqual(recoveries[1]["session_id"], "s-new")
            self.assertEqual(recoveries[1]["turn"], 4)
            self.assertEqual(recoveries[1]["sequence"], 2)
            self.assertTrue(daemon.sessions[SESSION_KEY]["recovered"])

    def test_stream_evidence_prevents_duplicate_recovery_after_crash(self) -> None:
        # Simulate the crash window: the recovery record was appended, but the
        # controller died before persisting `recovered` in the state file.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "write.json"
            events_path = root / EVENTS_FILE
            controller.atomic_json(
                state_path,
                {"version": 1, "sessions": {SESSION_KEY: {"session_id": "s-old", "status": "running", "active_turn": 2}}},
            )
            stream = controller.SupervisionEvents(events_path, "write")
            stream.append(
                kind="turn_failed",
                session_key=SESSION_KEY,
                session_id_value="s-old",
                turn=2,
                reason="controller-restart-recovery",
            )
            daemon = self.make_restarted_daemon(state_path, root)
            events = read_events(events_path)
            recoveries = [
                e for e in events
                if e["kind"] == "turn_failed" and e.get("reason") == "controller-restart-recovery"
            ]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0]["session_key"], SESSION_KEY)
            self.assertTrue(daemon.sessions[SESSION_KEY]["recovered"])
            # The sequence continues above the surviving recovery record.
            class TextSdk:
                def prompt(self, identifier, text, timeout):
                    return "ok"

            daemon.sdk = TextSdk()
            with mock.patch.object(controller, "repository_facts", return_value={"available": False}), mock.patch.object(
                controller, "wait_session_stats", return_value={"context_tokens": 10, "context_window": 1000}
            ):
                response = daemon.handle(
                    {"action": "prompt", "session_key": "repo:issue-7:writer", "prompt": "work", "timeout": 30}
                )
            self.assertEqual(response["event"]["sequence"], 3)
            self.assertEqual(response["event"]["event_id"], "write:3")
            events = read_events(events_path)
            self.assertEqual([e["kind"] for e in events], ["turn_failed", "turn_started", "turn_completed"])
            self.assertEqual([e["sequence"] for e in events], [1, 2, 3])

    def test_second_restart_does_not_duplicate_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "write.json"
            controller.atomic_json(
                state_path,
                {"version": 1, "sessions": {SESSION_KEY: {"session_id": "s-old", "status": "running", "active_turn": 1}}},
            )
            self.make_restarted_daemon(state_path, root)
            second = self.make_restarted_daemon(state_path, root)
            events = read_events(root / EVENTS_FILE)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["kind"], "turn_failed")
            record = second.sessions[SESSION_KEY]
            self.assertEqual(
                sorted(record),
                [
                    "previous_active_turn",
                    "previous_session_id",
                    "previous_status",
                    "recovered",
                    "restarted",
                ],
            )
            self.assertTrue(record["recovered"])


if __name__ == "__main__":
    unittest.main()

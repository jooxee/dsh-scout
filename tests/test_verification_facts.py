"""Tests for the verification-handoff protocol in run_dsh_session.py."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "scripts/run_dsh_session.py"
SPEC = importlib.util.spec_from_file_location("run_dsh_session", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


def _result(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


# Scripted git outputs keyed by the argument list handed to subprocess.run.
GIT_SCRIPT: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
    ("rev-parse", "--show-toplevel"): _result("/repo/worktree"),
    ("rev-parse", "HEAD"): _result("0123456789abcdef0123456789abcdef01234567"),
    ("branch", "--show-current"): _result("main"),
    ("status", "--porcelain=v1"): _result(" M a.py\n M b.py\n"),
    ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): _result("origin/main"),
    ("rev-list", "--left-right", "--count", "origin/main...HEAD"): _result("1\t2\n"),}


def make_fake_run(script: dict):
    def fake_run(args, **kwargs):
        return script.get(tuple(args[1:]), _result("", returncode=1, stderr="git: not found"))
    return fake_run


fake_run = make_fake_run(GIT_SCRIPT)


class RepositoryFactsTests(unittest.TestCase):
    def test_git_worktree_snapshot_is_controller_derived(self):
        with mock.patch.object(controller.subprocess, "run", side_effect=fake_run):
            facts = controller.repository_facts(Path("/irrelevant"))
        self.assertTrue(facts["available"])
        self.assertEqual(facts["repository_root"], "/repo/worktree")
        self.assertEqual(facts["head"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(facts["branch"], "main")
        self.assertEqual(facts["pending_changes"], 2)
        self.assertEqual(facts["porcelain_status"], [" M a.py", " M b.py"])
        self.assertFalse(facts["status_truncated"])
        self.assertFalse(facts["clean"])
        # `git rev-list --left-right --count upstream...HEAD` prints
        # "<commits only in upstream>\t<commits only in HEAD>", i.e.
        # behind \t ahead. The scripted output 1\t2 means the local branch
        # is 1 commit behind origin/main and 2 commits ahead of it.
        self.assertEqual(
            facts["upstream"],
            {"name": "origin/main", "behind": 1, "ahead": 2},
        )
        self.assertEqual(facts["upstream"]["behind"], 1)
        self.assertEqual(facts["upstream"]["ahead"], 2)

    def test_status_is_bounded_and_clipped(self):
        script = dict(GIT_SCRIPT)
        wide = [" M " + "x" * 500] * 300
        script[("status", "--porcelain=v1")] = _result("\n".join(wide))
        with mock.patch.object(controller.subprocess, "run", side_effect=make_fake_run(script)):
            facts = controller.repository_facts(Path("/irrelevant"))
        self.assertEqual(len(facts["porcelain_status"]), controller.GIT_FACTS_STATUS_LIMIT)
        self.assertTrue(all(len(line) <= controller.GIT_FACTS_STATUS_LINE_LIMIT for line in facts["porcelain_status"]))
        self.assertEqual(facts["pending_changes"], 300)
        self.assertTrue(facts["status_truncated"])

    def test_non_git_directory_records_bounded_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            facts = controller.repository_facts(Path(temporary))
        self.assertFalse(facts["available"])
        self.assertEqual(facts["status"], "not-a-git-worktree")
        self.assertIn("error", facts)
        self.assertLessEqual(len(facts["error"]), controller.GIT_FACTS_ERROR_LIMIT)

    def test_git_timeout_is_swallowed_as_fallback(self):
        with mock.patch.object(
            controller.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("git", 10),
        ):
            facts = controller.repository_facts(Path("/irrelevant"))
        self.assertFalse(facts["available"])
        self.assertEqual(facts["status"], "git-error")
        self.assertIn("error", facts)

    def test_snapshot_never_contains_prohibited_material(self):
        with mock.patch.object(controller.subprocess, "run", side_effect=fake_run):
            facts = controller.repository_facts(Path("/irrelevant"))
        raw = repr(facts)
        for prohibited in ("prompt", "response text", "PATCH"):
            self.assertNotIn(prohibited, raw)
        self.assertNotIn("content", facts)


def make_daemon(state_path: Path, root: Path):
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
            self.text = text
            return "handoff ready"

    daemon.sdk = FakeSdk()
    return daemon


class TurnLifecycleTests(unittest.TestCase):
    def test_dispatch_to_idle_records_facts_and_pending_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "writer.json"
            daemon = make_daemon(state_path, root)
            snapshots = {
                "pre": {"available": True, "branch": "main", "head": "a" * 40, "clean": True},
                "post": {
                    "available": True,
                    "branch": "main",
                    "head": "b" * 40,
                    "clean": False,
                    "pending_changes": 3,
                },
            }
            with (
                mock.patch.object(
                    controller,
                    "repository_facts",
                    side_effect=[snapshots["pre"], snapshots["post"]],
                ),
                mock.patch.object(
                    controller,
                    "wait_session_stats",
                    return_value={"context_tokens": 10, "context_window": 100},
                ),
            ):
                response = daemon.handle(
                    {
                        "action": "prompt",
                        "session_key": "repo:issue-1:writer",
                        "prompt": "Do a bounded task.",
                        "timeout": 5,
                    }
                )
            saved = controller.read_json(state_path)["sessions"]["repo:issue-1:writer"]
            self.assertEqual(saved["status"], "idle")
            self.assertEqual(saved["handoff_pre_facts"], snapshots["pre"])
            self.assertEqual(saved["handoff_post_facts"], snapshots["post"])
            self.assertEqual(saved["verification_facts"]["pre_prompt"], snapshots["pre"])
            self.assertEqual(saved["verification_facts"]["post_prompt"], snapshots["post"])
            self.assertEqual(saved["verification"]["state"], "pending")
            self.assertEqual(response["verification_state"], "pending")
            self.assertEqual(response["verification_facts"]["post_prompt"], snapshots["post"])
            self.assertIn("state_file", response)

    def test_verification_state_is_always_pending_after_completion(self):
        # DSH completion is never treated as proof that a repository task is
        # correct: every completed turn is recorded as requiring independent
        # orchestrator verification.
        self.assertEqual(controller.SessionDaemon._noop_verification_note(), "pending")

    def test_non_git_cwd_does_not_break_turn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "writer.json"
            daemon = make_daemon(state_path, root)
            fallback = {"available": False, "status": "not-a-git-worktree", "error": "fatal: not a git repository"}
            with (
                mock.patch.object(controller, "repository_facts", return_value=fallback),
                mock.patch.object(
                    controller,
                    "wait_session_stats",
                    return_value={"context_tokens": 0, "context_window": 100},
                ),
            ):
                response = daemon.handle(
                    {
                        "action": "prompt",
                        "session_key": "repo:issue-1:writer",
                        "prompt": "Work here.",
                        "timeout": 5,
                    }
                )
            saved = controller.read_json(state_path)["sessions"]["repo:issue-1:writer"]
            self.assertEqual(saved["handoff_pre_facts"], fallback)
            self.assertEqual(saved["verification"]["state"], "pending")
            self.assertEqual(response["verification_state"], "pending")


if __name__ == "__main__":
    unittest.main()

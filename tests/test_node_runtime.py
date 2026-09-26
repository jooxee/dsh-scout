import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import node_runtime


class NodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"PATH": str(self.root / "system"), "HOME": str(self.root)}

    def node(self, relative, compatible=True):
        path = self.root / relative / "node"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit " + ("0" if compatible else "1") + "\n")
        path.chmod(0o755)
        return path

    def test_old_path_falls_back_in_numeric_order(self):
        self.node("system", False)
        self.node(".nvm/versions/node/v9.9.9/bin")
        expected = self.node(".nvm/versions/node/v22.9.0/bin")
        self.node(".nvm/versions/node/v22.10.0/bin", False)
        self.assertEqual(node_runtime.select_node(self.env), expected)

    def test_compatible_path_wins(self):
        expected = self.node("system")
        self.node(".nvm/versions/node/v24.1.0/bin")
        self.assertEqual(node_runtime.select_node(self.env), expected)

    def test_explicit_override_wins(self):
        self.node("system")
        expected = self.node("chosen")
        self.env["DSH_SCOUT_NODE"] = str(expected)
        self.assertEqual(node_runtime.select_node(self.env), expected)

    def test_invalid_override_never_falls_back(self):
        self.node("system")
        for value in [
            "relative/node",
            str(self.root / "absent/node"),
            str(self.node("bad", False)),
        ]:
            with self.subTest(value=value):
                self.env["DSH_SCOUT_NODE"] = value
                with self.assertRaisesRegex(
                    node_runtime.NodeRuntimeError, "DSH_SCOUT_NODE"
                ):
                    node_runtime.select_node(self.env)

    def test_nvm_dir_and_child_environment(self):
        expected = self.node("custom/versions/node/v22.1.0/bin")
        self.env.update(NVM_DIR=str(self.root / "custom"), CODEX_HOME="/unchanged")
        node_runtime.configure_node(self.env)
        self.assertEqual(self.env["PATH"].split(os.pathsep)[0], str(expected.parent))
        self.assertEqual(self.env["CODEX_HOME"], "/unchanged")
        self.assertEqual(subprocess.run(["node"], env=self.env).returncode, 0)

    def test_missing_or_timed_out_runtime_has_safe_error(self):
        with self.assertRaisesRegex(node_runtime.NodeRuntimeError, "Node 22"):
            node_runtime.select_node(self.env)
        self.node("system")
        with mock.patch.object(
            node_runtime.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("private", 5),
        ):
            with self.assertRaises(node_runtime.NodeRuntimeError) as error:
                node_runtime.select_node(self.env)
        self.assertNotIn("private", str(error.exception))

    def test_public_launcher_rejects_override_before_creating_state(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("must not dispatch")
        state = self.root / "state.json"
        env = dict(os.environ, DSH_SCOUT_NODE=str(self.root / "missing/node"))
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "scripts/run_dsh_session.py"),
                "--mode",
                "write",
                "--cwd",
                str(self.root),
                "--session-key",
                "test",
                "--prompt-file",
                str(prompt),
                "--state-file",
                str(state),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("DSH_SCOUT_NODE", result.stderr)
        self.assertFalse(state.exists())

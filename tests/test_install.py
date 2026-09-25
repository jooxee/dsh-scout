"""Exercise installed packages in isolated homes without starting a model."""

import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts/install.sh"
PRESETS = {
    "codex": ".codex/skills/dsh-scout",
    "claude-code": ".claude/skills/dsh-scout",
    "opencode": ".config/opencode/skills/dsh-scout",
    "pi": ".pi/agent/skills/dsh-scout",
    "omp": ".omp/agent/skills/dsh-scout",
    "cursor": ".cursor/skills/dsh-scout",
}


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="scout install ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("DSH_", "CODEX_", "XDG_"))
        }
        self.env.update(HOME=str(self.home), XDG_RUNTIME_DIR=str(self.root / "runtime"))

    def run_command(self, *args):
        return subprocess.run(
            [str(arg) for arg in args], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=15,
        )

    def install(self, *args):
        result = self.run_command(INSTALL, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def assert_package(self, destination):
        payload = [ROOT / "SKILL.md", ROOT / "agents/openai.yaml", ROOT / "LICENSE"]
        payload += [ROOT / "scripts" / name for name in (
            "run-dsh-agent.sh", "run_dsh_session.py", "watch_dsh_events.py",
        )]
        payload += list((ROOT / "scripts/dsh_web").glob("*.py"))
        payload += list((ROOT / "docs/specifications").glob("*.md"))
        payload += [ROOT / "docs/integrations.md"]
        for source in payload:
            target = destination / source.relative_to(ROOT)
            self.assertEqual(source.read_bytes(), target.read_bytes(), str(target))
        for name in ("run-dsh-agent.sh", "watch_dsh_events.py"):
            executable = destination / "scripts" / name
            self.assertTrue(os.access(executable, os.X_OK))
            result = self.run_command(executable, "--help")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--mode", result.stdout)

    def test_all_presets_install_complete_independent_packages(self):
        for host, relative in PRESETS.items():
            with self.subTest(host=host):
                self.install("--host", host)
                self.assert_package(self.home / relative)

    def test_legacy_default_and_force_preserve_unrelated_files(self):
        self.install()
        destination = self.home / PRESETS["codex"]
        skill = destination / "SKILL.md"
        skill.write_text("local edit")
        sentinel = destination / "user-note.txt"
        sentinel.write_text("keep")
        result = self.run_command(INSTALL)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(skill.read_text(), "local edit")
        self.install("--force")
        self.assert_package(destination)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_custom_destination_spaces_and_relative_path(self):
        self.install("--destination", "custom root/dsh-scout/")
        self.assert_package(self.root / "custom root/dsh-scout")
        self.assertFalse((self.home / ".codex").exists())

    def test_environment_overrides(self):
        self.env["CODEX_HOME"] = str(self.root / "custom codex")
        self.env["XDG_CONFIG_HOME"] = str(self.root / "custom config")
        self.install()
        self.install("--host", "opencode")
        self.assert_package(self.root / "custom codex/skills/dsh-scout")
        self.assert_package(self.root / "custom config/opencode/skills/dsh-scout")

    def test_invalid_arguments_do_not_write(self):
        bad_arguments = [
            ("--host",), ("--destination",), ("--host", "unknown"),
            ("--host", "codex", "--destination", "dsh-scout"),
            ("--destination", "wrong-name"), ("--destination", ""),
            ("--host", "codex", "--host", "pi"),
            ("--destination", "dsh-scout", "--destination", "dsh-scout"),
            ("--destination", "--force"), ("--unexpected",), ("unexpected",),
        ]
        for args in bad_arguments:
            with self.subTest(args=args):
                result = self.run_command(INSTALL, *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(list(self.home.iterdir()), [])
                self.assertFalse((self.root / "dsh-scout").exists())

    def test_existing_directory_and_file_are_preserved(self):
        destination = self.root / "dsh-scout"
        destination.write_text("existing file")
        result = self.run_command(INSTALL, "--destination", destination, "--force")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(destination.read_text(), "existing file")
        destination.unlink()
        destination.mkdir()
        sentinel = destination / "user-note"
        sentinel.write_text("keep")
        result = self.run_command(INSTALL, "--destination", destination)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_help_does_not_install(self):
        for flag in ("--help", "-h", "help"):
            result = self.run_command(INSTALL, flag)
            self.assertEqual(result.returncode, 0)
            self.assertIn("cursor", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_installed_copies_share_writer_and_reader_locks(self):
        self.install("--host", "cursor")
        self.install("--host", "pi")
        prompt = self.root / "prompt.txt"
        prompt.write_text("Must never be dispatched")
        runtime = self.home / ".codex/state/dsh-scout"
        runtime.mkdir(parents=True)
        for mode in ("write", "read"):
            with (runtime / f"{mode}.request.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for host in ("cursor", "pi"):
                    self.env["XDG_RUNTIME_DIR"] = str(self.root / host / "runtime")
                    launcher = self.home / PRESETS[host] / "scripts/run-dsh-agent.sh"
                    result = self.run_command(
                        launcher, "--mode", mode, "--cwd", self.root,
                        "--session-key", "install:lock-check", "--prompt-file", prompt,
                    )
                    self.assertEqual(result.returncode, 75, result.stderr)
                    self.assertIn("already running", result.stderr)
                    self.assertEqual(result.stdout, "")
        self.assertFalse((runtime / "write.sock").exists())
        self.assertFalse((runtime / "read.sock").exists())
        self.assertFalse((runtime / "write.json").exists())
        self.assertFalse((runtime / "read.json").exists())


if __name__ == "__main__":
    unittest.main()

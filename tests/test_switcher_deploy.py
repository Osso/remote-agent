"""Local switcher installation must not touch remote workers or private maps."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SwitcherDeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="switcher-deploy-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True)
        self.hosts = config / "remote-hosts.json"
        self.hosts.write_text('{"agent-server":{"ssh":"must-not-connect"}}\n')
        self.environment = {**os.environ, "HOME": str(self.home)}
        self.link = self.home / ".local/bin/agent-switcher"

    def deploy(self):
        return subprocess.run(
            [str(ROOT / "deploy.sh"), "--switcher"],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_installs_and_repeats_without_changing_maps_or_remote_executable(self):
        original = self.hosts.read_bytes()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.resolve(), ROOT / "agent-switcher")
        self.assertEqual(self.hosts.read_bytes(), original)
        self.assertFalse((self.home / ".local/bin/remote-agent").exists())
        self.assertEqual(self.deploy().returncode, 0)
        help_result = subprocess.run(
            [str(self.link), "--help"],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--list", help_result.stdout)

    def test_refuses_to_replace_an_unmanaged_file(self):
        self.link.parent.mkdir(parents=True)
        self.link.write_text("keep my command\n")
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmanaged", result.stderr)
        self.assertEqual(self.link.read_text(), "keep my command\n")

    def test_refuses_to_replace_a_foreign_symlink(self):
        self.link.parent.mkdir(parents=True)
        original = self.home / "other-command"
        original.write_text("preserve\n")
        self.link.symlink_to(original)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign", result.stderr)
        self.assertEqual(self.link.resolve(), original)


if __name__ == "__main__":
    unittest.main()

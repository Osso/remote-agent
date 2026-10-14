"""Account freshness regressions using isolated OAuth-shaped credentials."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from test_remote_agent import isolate_home, load_worker


class ClaudeAccountFreshnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        isolate_home(self, self.home)
        self.module = load_worker()
        self.accounts = self.module.ClaudeAccounts(self.home)
        self.accounts.dir.mkdir(mode=0o700)
        self.accounts.owner_file.write_text("named")
        self.named = self.accounts.profile_path("named")
        self.other = self.accounts.profile_path("other")
        self.old = 1_700_000_000_000_000_000
        self.new = self.old + 10_000_000_000
        self.write_credentials(self.other, "other", self.old)

    def credentials(self, generation):
        return json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": f"fixture-access-{generation}",
                    "refreshToken": f"fixture-refresh-{generation}",
                    "expiresAt": 4102444800000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "team",
                }
            }
        ).encode()

    def write_credentials(self, path, generation, mtime_ns):
        path.write_bytes(self.credentials(generation))
        path.chmod(0o600)
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def test_switch_does_not_overwrite_newer_named_refresh(self):
        self.write_credentials(self.accounts.active, "R1", self.old)
        self.write_credentials(self.named, "R2", self.new)
        before = self.named.stat()

        self.accounts.use("other")

        self.assertEqual(self.named.read_bytes(), self.credentials("R2"))
        self.assertEqual(self.named.stat().st_ino, before.st_ino)
        self.assertEqual(self.named.stat().st_mtime_ns, self.new)
        self.assertEqual(self.accounts.active.read_bytes(), self.credentials("other"))
        self.assertEqual(self.accounts.active.stat().st_mtime_ns, self.old)
        self.assertEqual(self.accounts.owner(), "other")

    def test_switch_persists_newer_native_global_refresh(self):
        self.write_credentials(self.named, "R1", self.old)
        self.write_credentials(self.accounts.active, "R2", self.new)

        self.accounts.use("other")

        self.assertEqual(self.named.read_bytes(), self.credentials("R2"))
        self.assertEqual(self.named.stat().st_mtime_ns, self.new)
        self.assertEqual(stat.S_IMODE(self.named.stat().st_mode), 0o600)
        self.assertEqual(self.accounts.active.stat().st_mtime_ns, self.old)

    def test_same_profile_use_adopts_newer_named_refresh(self):
        self.write_credentials(self.accounts.active, "R1", self.old)
        self.write_credentials(self.named, "R2", self.new)
        before = self.named.stat()

        self.accounts.use("named")

        self.assertEqual(self.accounts.active.read_bytes(), self.credentials("R2"))
        self.assertEqual(self.accounts.active.stat().st_mtime_ns, self.new)
        self.assertEqual(self.named.stat().st_ino, before.st_ino)
        self.assertEqual(self.named.stat().st_mtime_ns, self.new)
        self.assertEqual(self.accounts.owner(), "named")
        self.assertEqual(stat.S_IMODE(self.accounts.active.stat().st_mode), 0o600)

    def test_identical_save_leaves_named_inode_and_mtime_unchanged(self):
        self.write_credentials(self.named, "R2", self.old)
        self.write_credentials(self.accounts.active, "R2", self.new)
        before = self.named.stat()

        self.assertEqual(self.accounts.save_active(), "named")

        self.assertEqual(self.named.read_bytes(), self.credentials("R2"))
        self.assertEqual(self.named.stat().st_ino, before.st_ino)
        self.assertEqual(self.named.stat().st_mtime_ns, self.old)
        self.assertEqual(self.accounts.active.stat().st_mtime_ns, self.new)

    def test_equal_mtime_different_bytes_prefers_global(self):
        self.write_credentials(self.named, "named", self.old)
        self.write_credentials(self.accounts.active, "global", self.old)

        self.accounts.use("named")

        self.assertEqual(self.named.read_bytes(), self.credentials("global"))
        self.assertEqual(self.accounts.active.read_bytes(), self.credentials("global"))
        self.assertEqual(self.named.stat().st_mtime_ns, self.old)
        self.assertEqual(self.accounts.active.stat().st_mtime_ns, self.old)

    def test_release_preserves_newer_named_refresh(self):
        self.write_credentials(self.accounts.active, "R1", self.old)
        self.write_credentials(self.named, "R2", self.new)

        self.accounts.release()

        self.assertEqual(self.named.read_bytes(), self.credentials("R2"))
        self.assertEqual(self.named.stat().st_mtime_ns, self.new)
        self.assertFalse(self.accounts.active.exists())
        self.assertIsNone(self.accounts.owner())

    def test_save_creates_missing_named_profile_with_source_mtime(self):
        self.write_credentials(self.accounts.active, "R2", self.new)

        self.assertEqual(self.accounts.save_active(), "named")

        self.assertEqual(self.named.read_bytes(), self.credentials("R2"))
        self.assertEqual(self.named.stat().st_mtime_ns, self.new)
        self.assertEqual(stat.S_IMODE(self.named.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()

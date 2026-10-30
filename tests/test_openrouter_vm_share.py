import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "sandbox/openrouter-vm/share.py"
spec = importlib.util.spec_from_file_location("vm_share", SCRIPT)
share = importlib.util.module_from_spec(spec)
spec.loader.exec_module(share)


class GuestShareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "etc").mkdir()
        self.fstab = self.root / "etc/fstab"
        self.original = "# existing mounts\nUUID=abc / ext4 defaults 0 1\n"
        self.fstab.write_text(self.original)
        self.fstab.chmod(0o640)

    def test_configure_preserves_fstab_and_is_idempotent(self):
        share.configure(self.root)
        expected = (
            self.original
            + "wow-install /syncthing/World\\040of\\040Warcraft 9p ro,trans=virtio,version=9p2000.L,cache=none,nodev,nosuid 0 0\n"
        )
        self.assertEqual(self.fstab.read_text(), expected)
        self.assertEqual(self.fstab.stat().st_mode & 0o777, 0o640)
        self.assertEqual(
            (self.root / "etc/modules-load.d/wow-install.conf").read_text(),
            "9p\n9pnet_virtio\n",
        )
        self.assertEqual(
            (self.root / "syncthing/World of Warcraft").stat().st_mode & 0o777, 0o755
        )
        result = subprocess.run(
            [
                "findmnt",
                "--fstab",
                "--tab-file",
                str(self.fstab),
                "--noheadings",
                "--output",
                "TARGET",
                "--source",
                "wow-install",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "/syncthing/World of Warcraft")
        share.configure(self.root)
        self.assertEqual(self.fstab.read_text(), expected)

    def test_conflicting_source_or_target_is_rejected_without_changes(self):
        for entry in (
            "wow-install /other 9p ro 0 0",
            "other /syncthing/World\\040of\\040Warcraft 9p ro 0 0",
        ):
            with self.subTest(entry=entry):
                original = self.original + entry + "\n"
                self.fstab.write_text(original)
                with self.assertRaisesRegex(ValueError, "conflicting fstab"):
                    share.configure(self.root)
                self.assertEqual(self.fstab.read_text(), original)
                self.assertFalse((self.root / "etc/modules-load.d").exists())


if __name__ == "__main__":
    unittest.main()

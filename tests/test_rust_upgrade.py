"""Live native session identity survives a supported immutable binary upgrade.

Only the owned upgrade case is loaded. A/B are same-wire ELF images with distinct
trailers, installed by deploy.py into an isolated HOME. No real SSH or systemd
manager, account, provider, or model is used. Evidence remains in
/var/tmp/pi-resize-fix/rust-upgrade/.
"""

import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

import test_rust_attach as native

EVIDENCE = Path("/var/tmp/pi-resize-fix/rust-upgrade")
INSTALL = """
import base64, hashlib, json, pathlib, runpy, sys
support = runpy.run_path(sys.argv[1])['RUST_SUPPORT']
namespace = {}
exec(support, namespace)
native = namespace['native_platform']()
suffix = '-unknown-linux-gnu' if native['os'] == 'Linux' else '-apple-darwin'
content = pathlib.Path(sys.argv[2]).read_bytes()
payload = {
    'target': native['arch'] + suffix,
    'binary': base64.b64encode(content).decode(),
    'sha256': hashlib.sha256(content).hexdigest(),
}
print(json.dumps(namespace['install_binary'](pathlib.Path.home(), payload)))
"""


class NativeRustUpgradeTests(native.NativeRustAttachTests):
    def setUp(self):
        native.EVIDENCE = EVIDENCE
        super().setUp()

    def install_image(self, image):
        result = subprocess.run(
            [sys.executable, "-c", INSTALL, str(native.ROOT / "deploy.py"), str(image)],
            env=self.env,
            cwd=native.ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.record(
            "install",
            image=str(image),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = Path(json.loads(result.stdout)["installed"])
        self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE(installed.parent.stat().st_mode), 0o500)
        self.assertEqual(stat.S_IMODE(installed.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(installed.stat().st_uid, os.geteuid())
        self.assertEqual(self.binary.resolve(), installed)
        return installed

    def test_immutable_upgrade_preserves_live_session_until_owned_cancel(self):
        content = self.binary.read_bytes()
        self.assertEqual(content[:4], b"\x7fELF")
        image_a = self.root / "image-a"
        image_b = self.root / "image-b"
        image_a.write_bytes(content + b"\nremote-agent upgrade fixture A\n")
        image_b.write_bytes(content + b"\nremote-agent upgrade fixture B\n")
        stable = self.root / ".local/bin/remote-agent-rs"
        self.assertTrue(stable.is_symlink())
        self.assertEqual(stable.readlink(), self.binary)
        stable.unlink()  # Remove only the base fixture's owned foreign symlink.
        self.binary = stable  # Deliberately retain the stable path across upgrade.
        installed_a = self.install_image(image_a)
        try:
            started = self.cli("start", "demo", "bulk-proof")
            self.assertTrue(started["running"], self.server_log())
            self.owned.update((started["pid"], started["child_pid"]))
            runtime_path = self.state / "runtime.json"
            runtime_before = runtime_path.read_bytes()
            launch_before = (self.state / "launch.json").read_bytes()
            server_exe = Path(f"/proc/{started['pid']}/exe")
            self.assertTrue(os.path.samefile(server_exe, installed_a))
            self.record("started-a", status=started, runtime=json.loads(runtime_before))
            self.attach()
            self.capture("ready")
            self.detach()

            installed_b = self.install_image(image_b)
            self.assertNotEqual(installed_a, installed_b)
            self.assertFalse(os.path.samefile(installed_a, installed_b))
            self.assertTrue(os.path.samefile(server_exe, installed_a))
            self.assertFalse(os.path.samefile(server_exe, self.binary))
            after = self.cli("status", "bulk-proof")
            self.assertTrue(after["running"], "B must recognize the live A server")
            for field in ("pid", "child_pid", "conversation"):
                self.assertEqual(after[field], started[field])
            self.assertEqual(runtime_path.read_bytes(), runtime_before)
            self.assertEqual((self.state / "launch.json").read_bytes(), launch_before)
            self.attach()
            self.capture("reconnected", wait_phase=False)
            os.write(self.master, b"b")
            self.capture("bulk")
            self.assert_bulk(self.analyze(self.stages[-2:], "upgraded")[1], "bulk")
            self.assertEqual(
                {event["pid"] for event in self.events()}, {started["child_pid"]}
            )
            attached = self.cli("status", "bulk-proof")
            self.assertTrue(attached["running"])
            for field in ("pid", "child_pid", "conversation"):
                self.assertEqual(attached[field], started[field])
            self.assertTrue(os.path.samefile(server_exe, installed_a))
            self.detach()
            logs = self.cli("logs", "bulk-proof", check=False)
            self.assertEqual(logs.returncode, 0, logs.stderr)
            self.assertIn("BULK_LINE_079", logs.stdout)
            unit = json.loads((self.state / "fixture-unit.json").read_text())
            self.assertEqual(unit["pid"], started["pid"])
            cancelled = self.cli("cancel", "bulk-proof")
            self.assertEqual(cancelled["cancelled"], "bulk-proof")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for pid in self.owned - self.reaped:
                    try:
                        reaped, _ = os.waitpid(pid, os.WNOHANG)
                        if reaped:
                            self.reaped.add(reaped)
                    except ChildProcessError:
                        pass
                if not any(native.process_exists(pid) for pid in self.owned):
                    break
                time.sleep(0.05)
            survivors = [pid for pid in self.owned if native.process_exists(pid)]
            self.record("cancelled-by-b", survivors=survivors, unit=unit)
            self.assertEqual(survivors, [])
            self.assertFalse(self.cli("status", "bulk-proof")["running"])
            self.assertEqual(runtime_path.read_bytes(), runtime_before)
            self.assertEqual((self.state / "launch.json").read_bytes(), launch_before)
            self.assertTrue((self.state / "server.log").is_file())
            self.record("behavior", result="passed", status=attached)
        finally:
            # The RED binary can own-cancel A only after restoring the A link.
            self.install_image(image_a)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        [
            NativeRustUpgradeTests(
                "test_immutable_upgrade_preserves_live_session_until_owned_cancel"
            )
        ]
    )


if __name__ == "__main__":
    unittest.main()

"""Session unit OOM handling with the real systemd user manager.

Runs a transient user service with the repository unit's kill and OOM settings,
a small MemoryMax and a child that exceeds it. The session process must survive
the kernel OOM kill of that child, as Pi survives an OOM-killed tool process.
"""

import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLIED = ("KillMode", "OOMPolicy")


def unit_properties():
    lines = (ROOT / "remote-agent-rs@.service").read_text().splitlines()
    pairs = (line.split("=", 1) for line in lines if "=" in line)
    return [f"{key}={value}" for key, value in pairs if key in APPLIED]


def user_manager_available():
    if shutil.which("systemd-run") is None:
        return False
    probe = ["systemctl", "--user", "is-system-running"]
    state = subprocess.run(
        probe, check=False, capture_output=True, text=True
    ).stdout.strip()
    return state in ("running", "degraded")


@unittest.skipUnless(user_manager_available(), "needs a systemd user manager")
class SessionUnitOomTests(unittest.TestCase):
    def test_session_process_survives_oom_killed_child(self):
        self.assertEqual(len(unit_properties()), len(APPLIED))
        with tempfile.TemporaryDirectory(prefix="unit-oom-") as temp:
            marker = Path(temp) / "survived"
            # The sleep gives systemd time to apply OOMPolicy before the marker.
            script = f"python3 -c 'bytearray(512 << 20)'; sleep 3; touch {marker}"
            command = ["systemd-run", "--user", "--wait", "--collect", "--quiet"]
            command += ["--unit", f"remote-agent-oom-test-{uuid.uuid4().hex[:8]}"]
            for prop in unit_properties() + ["MemoryMax=64M", "MemorySwapMax=0"]:
                command += ["-p", prop]
            command += ["sh", "-c", script]
            result = subprocess.run(
                command, check=False, capture_output=True, text=True, timeout=60
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()

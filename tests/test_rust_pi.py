#!/usr/bin/env python3
"""Opt-in installed-Pi acceptance; offline, isolated HOME, and no submitted prompt."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import test_rust_attach as fixture


class InstalledPiAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        value = os.environ.get("REMOTE_AGENT_PI_BIN")
        if not value or not Path(value).is_file():
            raise unittest.SkipTest(
                "set REMOTE_AGENT_PI_BIN to the installed Pi executable"
            )
        cls.pi = str(Path(value).resolve())
        fixture.NativeRustAttachTests.setUpClass()

    def setUp(self):
        self.harness = fixture.NativeRustAttachTests(
            "test_bulk_output_and_reconnect_preserve_real_pi_history"
        )
        self.harness.setUp()
        self.addCleanup(self.harness.cleanup_session)
        harness = self.harness
        agent = harness.root / ".config/pi/agent"
        agent.mkdir(parents=True)
        harness.env.update(
            PI_CODING_AGENT_DIR=str(agent),
            PI_OFFLINE="1",
            WAYLAND_DISPLAY="fixture-wayland",
        )
        wrapper = harness.root / ".local/bin/pi-fixture"
        arguments = [
            self.pi,
            "--offline",
            "--session-dir",
            str(harness.root / "pi-sessions"),
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--provider",
            "openai",
            "--model",
            "gpt-4o-mini",
        ]
        wrapper.write_text(
            f"#!{sys.executable}\nimport os,sys\n"
            f"command = [{self.pi!r}, *sys.argv[1:]] if sys.argv[1:2] == ['login'] else {arguments!r} + sys.argv[1:]\n"
            f"os.execv({self.pi!r}, command)\n"
        )
        (harness.root / ".local/bin/pi").symlink_to(wrapper)
        png = fixture.ROOT / "tests/fixtures/clipboard-rgba.png"
        clipboard = harness.root / ".local/bin/wl-paste"
        clipboard.write_text(
            f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
            f"sys.stdout.buffer.write(Path({str(png)!r}).read_bytes())\n"
        )
        clipboard.chmod(0o700)
        self.png = png.read_bytes()

    def test_new_installed_pi_session_accepts_transparent_image_paste(self):
        harness = self.harness
        started = harness.cli("start", "demo", "bulk-proof")
        harness.owned.update((started["pid"], started["child_pid"]))
        harness.attach()
        output = bytearray()
        for _ in range(100):
            output.extend(harness.drain(0.1))
            if b"\x1b[3J" in output:
                break
        output.extend(harness.drain(1))
        status = harness.cli("status", "bulk-proof")
        self.assertTrue(
            status["running"],
            f"installed Pi exited: {status}\n{output.decode(errors='replace')}\n{harness.server_log()}",
        )
        os.write(harness.master, b"\x16")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            output.extend(harness.drain(0.1))
            images = list((harness.state / "images").glob("*.png"))
            if images and b"pi-clipboard-" in output:
                break
        images = list((harness.state / "images").glob("*.png"))
        self.assertEqual(len(images), 1, harness.server_log())
        self.assertEqual(images[0].read_bytes(), self.png)
        reports = harness.analyze(
            [{"name": "installed-pi-paste", "output": output.decode(errors="replace")}],
            "installed-pi",
        )
        visible = "\n".join(reports[0]["visible"])
        self.assertIn(
            "pi-clipboard-",
            visible,
            "the real Pi editor must display the transferred image reference",
        )
        harness.detach()
        after = harness.cli("status", "bulk-proof")
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(after[field], started[field])
        self.assertTrue(after["running"])
        harness.record(
            "installed-pi",
            result="passed",
            image=images[0].name,
            offline=True,
            submitted_prompt=False,
        )

    def test_provider_login_reaches_installed_pi_validation(self):
        harness = self.harness
        provider = "remote-agent-invalid-acceptance-provider"
        result = subprocess.run(
            [str(harness.binary), "__login", provider],
            env=harness.env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        harness.record(
            "installed-login-validation",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(provider, result.stdout + result.stderr)
        self.assertNotIn("unexpected SSH", result.stderr)
        auth = harness.root / ".config/pi/agent/auth.json"
        if auth.exists():
            self.assertEqual(
                json.loads(auth.read_text()),
                {},
                "invalid provider must not create credentials",
            )


if __name__ == "__main__":
    unittest.main()

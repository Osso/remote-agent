"""Installed Pi PTY regression: python3 -m unittest discover -s tests -p test_periodic_runtime.py -v.

Requires Pi 0.80.3+ on PATH (or PI_TEST_BINARY); uses no provider network,
credentials, dependencies, existing sessions, or installed configuration.
"""

import errno
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
PROVIDER = REPO / "tests/fixtures/periodic-runtime-provider.ts"
REPORT = "PERIODIC_RUNTIME_CHILD_REPORT"
ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


class RuntimeFixture:
    def __init__(self, mode):
        self.temp = tempfile.TemporaryDirectory(prefix="periodic-runtime-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.agent = self.root / "agent"
        self.sessions = self.root / "sessions"
        self.output = ""
        self.process = None
        self.master = None
        self.mode = mode

    def start(self):
        for path in (self.project, self.agent, self.sessions):
            path.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(self.project)], check=True, timeout=5
        )
        self.checklist = self.project / "periodic-checks.md"
        self.checklist.write_text(
            "---\ntype: Periodic Checks\ninterval_seconds: 1\n---\n"
            "Read-only test audit. Report findings; no edits or nested children.\n"
        )
        extensions = self.agent / "extensions"
        extensions.mkdir()
        shutil.copyfile(PROVIDER, extensions / PROVIDER.name)
        (self.agent / "settings.json").write_text(
            json.dumps(
                {
                    "approvalPreset": "auto-approve",
                    "approvalPolicy": "auto-approve",
                    "quietStartup": True,
                    "retry": {"enabled": False},
                    "agents": {
                        "explore": {
                            "model": "periodic-runtime-faux/periodic-runtime-faux-1",
                            "context": "fresh",
                        }
                    },
                    "disabledExtensions": [
                        "claude-memory-enrich",
                        "claude-memory-session-end",
                        "session-autoname",
                        "goal",
                    ],
                    "compaction": {"enabled": False},
                }
            )
        )
        # Explicit allowlist: no inherited credentials, broker, relay, or real state paths.
        env = {
            key: os.environ[key]
            for key in ("PATH", "NODE_PATH", "LANG")
            if key in os.environ
        }
        env.update(
            {
                "HOME": str(self.root),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "PI_CODING_AGENT_DIR": str(self.agent),
                "PI_CODING_AGENT_STATE_DIR": str(self.agent),
                "PI_CODING_AGENT_SESSION_DIR": str(self.sessions),
                "PI_PERIODIC_TEST_DIRECTORY": str(self.root),
                "PI_OFFLINE": "1",
                "PI_TELEMETRY": "0",
                "TERM": "xterm-256color",
                "NO_COLOR": "1",
            }
        )
        binary = os.environ.get("PI_TEST_BINARY") or shutil.which("pi")
        if not binary:
            raise RuntimeError("Install Pi 0.80.3+ or set PI_TEST_BINARY")
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        try:
            self.process = subprocess.Popen(
                [
                    binary,
                    "--offline",
                    "--no-context-files",
                    "--no-skills",
                    "--no-themes",
                    "--no-prompt-templates",
                    "--extension",
                    str(REPO / "periodic-checks.ts"),
                    "--provider",
                    "periodic-runtime-faux",
                    "--model",
                    "periodic-runtime-faux-1",
                    "--session-dir",
                    str(self.sessions),
                    "--name",
                    "Disposable periodic runtime test",
                ],
                cwd=self.project,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        self.wait(
            lambda: bool(self.events("session_start")), "private Pi session startup"
        )
        self.send(f"PERIODIC_RUNTIME_MAIN HOLD_{self.mode.upper()}\r")
        self.wait(
            lambda: bool(self.events(f"main_{self.mode}_held")),
            f"held main {self.mode}",
        )

    def pump(self, seconds=0.05):
        if self.master is None:
            return
        ready, _, _ = select.select([self.master], [], [], seconds)
        if ready:
            try:
                data = os.read(self.master, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                data = b""
            self.output += data.decode("utf-8", errors="replace")

    def wait(self, predicate, description, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return
            if self.process.poll() is not None:
                raise AssertionError(
                    f"Pi exited {self.process.returncode}: {self.visible()[-6000:]}"
                )
        raise AssertionError(
            f"Timeout: {description}\nEvents: {self.events()}\nPTY: {self.visible()[-6000:]}"
        )

    def events(self, kind=None):
        path = self.root / "events.jsonl"
        entries = (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )
        return (
            entries
            if kind is None
            else [entry for entry in entries if entry["kind"] == kind]
        )

    def visible(self):
        return ANSI.sub("", self.output)

    def send(self, text):
        os.write(self.master, text.encode())

    def release(self, name):
        (self.root / f"release-{name}").touch()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
            self.pump(0)
        if self.master is not None:
            os.close(self.master)
        self.temp.cleanup()


@unittest.skipUnless(os.name == "posix", "Pi interactive runtime requires a POSIX PTY")
class PeriodicRuntimeTests(unittest.TestCase):
    def assert_busy(self, fixture):
        self.assertFalse(fixture.events(f"main_{fixture.mode}_released"))
        self.assertFalse(fixture.events("provider_error"))
        self.assertFalse(
            [
                event
                for event in fixture.events("agent_end")
                if event["agentId"] == "main"
            ]
        )
        self.assertIsNone(fixture.process.poll())

    def assert_live_periodic_audit(self, mode):
        fixture = RuntimeFixture(mode)
        self.addCleanup(fixture.close)
        fixture.start()
        if mode == "model":
            # No status command has been submitted: only the timer can launch this child.
            fixture.wait(
                lambda: bool(fixture.events("child_model_held")),
                "independent timer audit before any manual command",
            )
            self.assert_busy(fixture)
        initial_output = len(fixture.output)
        fixture.send("/periodic-checks status\r")
        fixture.wait(
            lambda: (
                "Periodic checks: armed"
                in ANSI.sub("", fixture.output[initial_output:])
            ),
            "visible manual status before main release",
            timeout=3,
        )
        self.assertIn("interval_seconds: 1", fixture.visible())
        self.assert_busy(fixture)
        fixture.wait(
            lambda: bool(fixture.events("child_model_held")),
            "timer-launched child report while parent remains active",
        )
        children = [
            entry
            for entry in fixture.events("session_start")
            if entry["agentId"] != "main"
        ]
        self.assertEqual(len(children), 1, "Exactly one real child session must start")
        deliveries = [
            entry
            for entry in fixture.events("tool_result")
            if entry["toolName"] == "send_agent_message"
        ]
        self.assertEqual(len(deliveries), 1)
        self.assertFalse(deliveries[0]["isError"], deliveries[0])
        self.assertIn(REPORT, json.dumps(fixture.events("mailbox_enqueued")))
        self.assertEqual(
            len(
                [
                    entry
                    for entry in fixture.events("agent_start")
                    if entry["agentId"] == "main"
                ]
            ),
            1,
            "Timer must not inject a second main prompt while held",
        )
        self.assert_busy(fixture)
        # Keep the child live across two more actual timer deadlines.
        deadline = time.monotonic() + 2.2
        while time.monotonic() < deadline:
            fixture.pump()
        self.assert_busy(fixture)
        self.assertEqual(
            len(
                [
                    entry
                    for entry in fixture.events("session_start")
                    if entry["agentId"] != "main"
                ]
            ),
            1,
        )
        self.assertEqual(len(fixture.events("child_provider_request")), 1)
        fixture.checklist.unlink()  # Only private test metadata; prevent a later idle tick.
        fixture.release("child")
        fixture.release("main")
        fixture.wait(
            lambda: bool(
                [
                    entry
                    for entry in fixture.events("agent_end")
                    if entry["agentId"] == "main"
                ]
            ),
            "normal main completion",
        )
        fixture.wait(
            lambda: REPORT in json.dumps(fixture.events("mailbox")),
            "parent mailbox receipt after normal main continuation",
        )
        self.assertEqual(len(fixture.events(f"main_{mode}_released")), 1)
        main_end = next(
            entry for entry in fixture.events("agent_end") if entry["agentId"] == "main"
        )
        self.assertIn("PERIODIC_RUNTIME_MAIN_FINISHED", json.dumps(main_end))
        self.assertNotIn('"stopReason": "aborted"', json.dumps(fixture.events()))
        self.assertFalse(fixture.events("provider_error"))
        errors = [entry for entry in fixture.events("tool_result") if entry["isError"]]
        self.assertEqual(errors, [], "Real runtime tool errors must not be hidden")

    def test_timer_and_manual_status_during_main_model(self):
        self.assert_live_periodic_audit("model")

    def test_timer_and_manual_status_during_main_tool(self):
        self.assert_live_periodic_audit("tool")

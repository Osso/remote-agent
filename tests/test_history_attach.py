"""Controller history preload over its existing SSH protocol, on a real PTY."""

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HistoryAttachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ra-history-attach-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True)
        (config / "remote-hosts.json").write_text(
            json.dumps(
                {
                    "agent-server": {
                        "ssh": "fixture-host",
                        "home": "/home/fixture",
                        "wrap": [],
                        "windows_shell": False,
                        "cargo_build_jobs": "2",
                    }
                }
            )
        )
        (config / "remote-projects.json").write_text("{}")
        self.events = self.home / "events.jsonl"
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "EVENTS": str(self.events),
            "HISTORY_MODE": "available",
            "MOSH_RETRY": "0",
            "PROJECT": "actual-project",
            "MOSH_INTERRUPT": "0",
            "STATUS_ERROR": "0",
        }
        ssh = self.bin / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json,os,sys
request=json.load(sys.stdin)
with open(os.environ['EVENTS'],'a') as f:
    f.write(json.dumps(request)+'\n')
if request['action']=='history':
    mode=os.environ['HISTORY_MODE']
    if mode=='error':
        print('export failed: unreadable saved conversation',file=sys.stderr)
        sys.exit(1)
    if mode=='unavailable':
        print(json.dumps({'available':False,'reason':'No saved conversation history yet.','project':os.environ['PROJECT'] or None}))
    else:
        history=''.join(f'\x1b[48;2;40;50;40mHISTORY-{i:04d}\x1b[0m\n' for i in range(1,201))
        print(json.dumps({'available':True,'history':history,'project':os.environ['PROJECT'] or None}))
else:
    if os.environ['STATUS_ERROR']=='1':
        print('status fixture failure',file=sys.stderr)
        sys.exit(1)
    print(json.dumps({'session':'remote-agent-demo','dead':False}))
"""
        )
        ssh.chmod(0o700)
        mosh = self.bin / "mosh"
        mosh.write_text(
            f"#!{sys.executable}\n"
            + r"""
import json,os,signal,sys
before=[]
if os.path.exists(os.environ['EVENTS']):
    with open(os.environ['EVENTS']) as f:
        before=[json.loads(line) for line in f]
with open(os.environ['EVENTS'],'a') as f:
    f.write(json.dumps({'action':'mosh','argv':sys.argv[1:]})+'\n')
sys.stdout.write('\x1b[H\x1b[2JLIVE-VIEW\r\n')
sys.stdout.flush()
if os.environ['MOSH_INTERRUPT']=='1':
    os.kill(os.getppid(), signal.SIGINT)
if os.environ['MOSH_RETRY']=='1' and not any(x['action']=='mosh' for x in before):
    sys.exit(5)
"""
        )
        mosh.chmod(0o700)

    def invoke(self, **environment):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "remote-agent.py"), "attach", "demo"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**self.env, **environment},
            start_new_session=True,
        )
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                elif process.poll() is not None:
                    break
            if process.poll() is None:
                process.wait(timeout=1)
            events = [json.loads(line) for line in self.events.read_text().splitlines()]
            return process.returncode, bytes(output), events
        finally:
            os.close(master)
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=3)

    def assert_title_lifecycle(self, output, attempts=1, project=b"actual-project"):
        push = b"\x1b[22;2t"
        pop = b"\x1b[23;2t"
        title = b"\x1b]2;" + project + b"\x07"
        self.assertEqual(output.count(push), 1, output)
        self.assertEqual(output.count(pop), 1, output)
        self.assertEqual(output.count(title), attempts, output)
        self.assertNotIn(b"\x1b]2;demo\x07", output)
        self.assertLess(output.index(push), output.index(title))
        titles = [i for i in range(len(output)) if output.startswith(title, i)]
        views = [i for i in range(len(output)) if output.startswith(b"LIVE-VIEW", i)]
        self.assertEqual(len(views), attempts)
        for title_at, view_at in zip(titles, views):
            self.assertLess(title_at, view_at)
        self.assertGreater(output.index(pop), views[-1])
        self.assertNotIn(b"\x1b[?1049l", output[output.index(pop) :])
        terminal = subprocess.run(
            ["node", str(ROOT / "tests/history_scrollback_fixture.cjs")],
            input=json.dumps({"output": output.decode(), "columns": 80, "rows": 12}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(terminal.stderr, "")
        titles = json.loads(terminal.stdout)["titles"]
        self.assertEqual(
            titles,
            ["previous-title", *([project.decode()] * attempts), "previous-title"],
        )

    def test_title_is_project_not_session_and_restored_on_detach(self):
        code, output, _ = self.invoke()
        self.assertEqual(code, 0, output)
        self.assert_title_lifecycle(output)

    def test_title_reapplied_on_retry_with_one_saved_title(self):
        code, output, _ = self.invoke(MOSH_RETRY="1")
        self.assertEqual(code, 0, output)
        self.assert_title_lifecycle(output, attempts=2)

    def test_title_available_without_history(self):
        code, output, _ = self.invoke(HISTORY_MODE="unavailable")
        self.assertEqual(code, 0, output)
        self.assert_title_lifecycle(output)

    def test_absent_project_does_not_invent_title(self):
        code, output, _ = self.invoke(HISTORY_MODE="unavailable", PROJECT="")
        self.assertEqual(code, 0, output)
        self.assertNotIn(b"\x1b]2;", output)
        self.assertNotIn(b"\x1b[22;2t", output)
        self.assertNotIn(b"\x1b[23;2t", output)

    def test_project_controls_cannot_inject_terminal_sequences(self):
        code, output, _ = self.invoke(PROJECT="safe\x1b]0;bad\x07\r\n\t\x7f\u009cname")
        self.assertEqual(code, 0, output)
        self.assert_title_lifecycle(output, project=b"safe]0;badname")

    def test_title_restored_after_status_failure(self):
        code, output, _ = self.invoke(STATUS_ERROR="1")
        self.assertNotEqual(code, 0, output)
        self.assert_title_lifecycle(output)

    def test_title_restored_after_keyboard_interrupt(self):
        code, output, _ = self.invoke(MOSH_INTERRUPT="1")
        self.assertNotEqual(code, 0, output)
        self.assert_title_lifecycle(output)

    def test_non_tty_skips_history_lookup_and_title_sequences(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "remote-agent.py"), "attach", "demo"],
            env=self.env,
            capture_output=True,
            timeout=8,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual([e["action"] for e in events], ["mosh", "status"])
        self.assertNotIn(b"\x1b]2;", result.stdout)
        self.assertNotIn(b"\x1b[22;2t", result.stdout)
        self.assertNotIn(b"\x1b[23;2t", result.stdout)

    def test_restores_complete_history_before_live_attachment(self):
        code, output, events = self.invoke()
        self.assertEqual(code, 0, output)
        self.assertEqual(
            [event["action"] for event in events], ["history", "mosh", "status"]
        )
        self.assertEqual(
            events[0],
            {
                "action": "history",
                "name": "demo",
                "columns": 80,
                "colorterm": "truecolor",
            },
        )
        positions = [output.index(f"HISTORY-{i:04d}".encode()) for i in range(1, 201)]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[-1], output.index(b"LIVE-VIEW"))
        self.assertIn(b"\x1b[48;2;40;50;40m", output)
        self.assertNotIn(b"\x1b[3J", output)
        self.assertIn("--no-init", events[1]["argv"])
        terminal = subprocess.run(
            ["node", str(ROOT / "tests/history_scrollback_fixture.cjs")],
            input=json.dumps({"output": output.decode(), "columns": 80, "rows": 12}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(terminal.returncode, 0, terminal.stdout + terminal.stderr)
        proof = json.loads(terminal.stdout)
        self.assertEqual(proof["markerCount"], 200)
        self.assertTrue(proof["ordered"])
        self.assertLess(proof["lastHistoryRow"], proof["liveViewportStart"])
        self.assertTrue(proof["liveViewPresent"])

    def test_reconnect_restores_history_again_before_live_connection(self):
        code, output, events = self.invoke(MOSH_RETRY="1")
        self.assertEqual(code, 0, output)
        self.assertEqual(
            [e["action"] for e in events],
            ["history", "mosh", "history", "mosh", "status"],
        )
        self.assertEqual(output.count(b"HISTORY-0001"), 2)
        self.assertEqual(output.count(b"HISTORY-0200"), 2)

    def test_no_saved_history_is_explicit_but_allows_live_attachment(self):
        code, output, events = self.invoke(HISTORY_MODE="unavailable")
        self.assertEqual(code, 0, output)
        self.assertIn(b"No saved conversation history yet.", output)
        self.assertIn(b"LIVE-VIEW", output)
        self.assertEqual([e["action"] for e in events], ["history", "mosh", "status"])

    def test_export_failure_does_not_silently_attach_without_history(self):
        code, output, events = self.invoke(HISTORY_MODE="error")
        self.assertNotEqual(code, 0, output)
        self.assertIn(b"export failed", output)
        self.assertEqual([e["action"] for e in events], ["history"])

"""Resize a real controller PTY while fake SSH and Mosh keep a server alive."""

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest

import test_history_attach as history_fixture

ROOT = history_fixture.ROOT

SSH = r"""
import json, os, sys
request = json.load(sys.stdin)
if request['action'] == 'history' and request['columns'] != 80:
    with open(os.environ['EVENTS']) as stream:
        old_pid = [json.loads(line)['pid'] for line in stream if json.loads(line)['action'] == 'mosh'][-1]
    try:
        os.kill(old_pid, 0)
        request['previous_mosh_alive'] = True
    except ProcessLookupError:
        request['previous_mosh_alive'] = False
with open(os.environ['EVENTS'], 'a') as stream:
    stream.write(json.dumps(request) + '\n')
if request['action'] == 'history':
    columns = request['columns']
    mode = os.environ['REFRESH_MODE'] if columns != 80 else os.environ['HISTORY_MODE']
    if mode == 'error':
        print('refresh export failed: unreadable saved conversation', file=sys.stderr)
        sys.exit(1)
    if mode == 'unavailable':
        print(json.dumps({'available': False, 'reason': 'No saved conversation history yet.', 'project': 'actual-project'}))
    else:
        history = f'SNAPSHOT-{columns}\n' + ''.join(f'HISTORY-{i:04d}\n' for i in range(1, 201))
        print(json.dumps({'available': True, 'history': history, 'project': 'actual-project'}))
elif request['action'] == 'status':
    os.kill(int(os.environ['SERVER_PID']), 0)
    print(json.dumps({'session': 'remote-agent-demo', 'dead': False}))
else:
    raise RuntimeError(f'unexpected server mutation: {request}')
"""

MOSH = r"""
import json, os, signal, sys, termios, tty

def event(action, **values):
    with open(os.environ['EVENTS'], 'a') as stream:
        stream.write(json.dumps({'action': action, 'pid': os.getpid(), **values}) + '\n')

def terminate(signum, frame):
    event('mosh-exit', reason='resize')
    print('OLD-MOSH-SHUTDOWN', flush=True)
    sys.exit(0)

saved = termios.tcgetattr(0)
signal.signal(signal.SIGTERM, terminate)
try:
    tty.setraw(0)
    columns, rows = os.get_terminal_size(0)
    print('\x1b[H\x1b[2JLIVE-VIEW\r\n', end='', flush=True)
    event('mosh', columns=columns, rows=rows, sid=os.getsid(0))
    while True:
        value = os.read(0, 1)
        if not value or value == b'\x1c':
            event('mosh-exit', reason='detach')
            break
        event('input', value=value.hex())
finally:
    termios.tcsetattr(0, termios.TCSANOW, saved)
"""

# Feed actual output chunks across the resize boundary, not just ANSI substrings.
TERMINAL_PROOF = r"""
const fs = require('node:fs');
const { Terminal } = require('@xterm/headless');
(async () => {
    const request = JSON.parse(fs.readFileSync(0, 'utf8'));
    const terminal = new Terminal({cols: 80, rows: 12, scrollback: 3000,
        allowProposedApi: true, windowOptions: {pushTitle: true, popTitle: true}});
    const titles = [];
    terminal.onTitleChange(title => titles.push(title));
    const write = value => new Promise(resolve => terminal.write(value, resolve));
    await write('\x1b]2;previous-title\x07' + 'UNRELATED-PREATTACH\r\n'.repeat(30));
    for (const phase of request) {
        terminal.resize(phase.columns, 12);
        await write(phase.output);
    }
    const buffer = terminal.buffer.normal;
    const lines = Array.from({length: buffer.length}, (_, i) => buffer.getLine(i).translateToString(true));
    console.log(JSON.stringify({lines, titles, liveViewportStart: buffer.baseY}));
    terminal.dispose();
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class ResizeAttachTests(unittest.TestCase):
    def setUp(self):
        history_fixture.HistoryAttachTests.setUp(self)
        for executable, source in [("ssh", SSH), ("mosh", MOSH)]:
            (self.bin / executable).write_text(f"#!{sys.executable}\n" + source)
        self.server = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self.addCleanup(self.stop_server)
        self.env.update(SERVER_PID=str(self.server.pid), REFRESH_MODE="available")
        self.master = None
        self.process = None
        self.output = bytearray()
        self.phases = []
        self.addCleanup(self.stop_attachment)

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
        self.server.wait(timeout=3)

    def events_so_far(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def stop_attachment(self):
        for event in self.events_so_far():
            if event["action"] == "mosh":
                try:
                    os.kill(event["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=3)
        if self.master is not None:
            os.close(self.master)

    def start_attachment(self, **environment):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 80, 0, 0))
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "remote-agent.py"), "attach", "demo"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**self.env, **environment},
            start_new_session=True,
        )
        os.close(slave)
        self.wait_event("mosh", 1)

    def drain(self, duration=0.05):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select(
                [self.master], [], [], min(0.02, max(0, deadline - time.monotonic()))
            )[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.output.extend(chunk)

    def wait_event(self, action, count, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            events = self.events_so_far()
            if sum(event["action"] == action for event in events) >= count:
                return events
            if self.process.poll() is not None:
                break
        self.fail(
            f"missing {action} event #{count}: {self.events_so_far()}; output={bytes(self.output)!r}"
        )

    def resize(self, columns):
        self.drain()
        self.phases.append(
            {
                "columns": 80 if not self.phases else self.columns,
                "output": self.output.decode(),
            }
        )
        self.output.clear()
        self.columns = columns
        fcntl.ioctl(
            self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 12, columns, 0, 0)
        )
        self.process.send_signal(signal.SIGWINCH)

    def finish(self):
        self.process.wait(timeout=3)
        self.drain()
        self.assertIsNone(self.server.poll(), "attachment killed the running server")
        modes = termios.tcgetattr(self.master)
        cooked_flags = termios.ICANON | termios.ECHO | termios.ISIG
        self.assertEqual(modes[3] & cooked_flags, cooked_flags)
        self.assertTrue(modes[1] & termios.OPOST)
        return self.events_so_far()

    def detach(self):
        os.write(self.master, b"\x1c")
        return self.finish()

    def combined_output(self):
        return "".join(phase["output"] for phase in self.phases).encode() + bytes(
            self.output
        )

    def test_resize_replaces_scrollback_after_fresh_history_and_preserves_input(self):
        self.start_attachment()
        os.write(self.master, b"A")
        self.wait_event("input", 1)
        self.resize(100)
        events = self.wait_event("mosh", 2)
        os.write(self.master, b"B")
        self.wait_event("input", 2)
        events = self.detach()
        self.assertEqual(self.process.returncode, 0, self.combined_output())
        self.assertEqual(
            [event["action"] for event in events],
            [
                "history",
                "mosh",
                "input",
                "mosh-exit",
                "history",
                "mosh",
                "input",
                "mosh-exit",
                "status",
            ],
        )
        starts = [event for event in events if event["action"] == "mosh"]
        self.assertEqual([event["columns"] for event in starts], [80, 100])
        self.assertNotEqual(starts[0]["pid"], starts[1]["pid"])
        for event in starts:
            self.assertEqual(
                event["pid"], event["sid"], "Mosh must own its process session"
            )
        histories = [event for event in events if event["action"] == "history"]
        self.assertEqual([event["columns"] for event in histories], [80, 100])
        self.assertFalse(histories[1]["previous_mosh_alive"])
        self.assertEqual(
            [event["value"] for event in events if event["action"] == "input"],
            ["41", "42"],
        )
        self.assertEqual(
            [event["reason"] for event in events if event["action"] == "mosh-exit"],
            ["resize", "detach"],
        )
        output = self.combined_output()
        self.assertNotIn(b"connection lost", output)
        self.assertEqual(output.count(b"\x1b[3J"), 1)
        self.assertLess(output.index(b"OLD-MOSH-SHUTDOWN"), output.index(b"\x1b[3J"))
        self.assertLess(output.index(b"\x1b[3J"), output.index(b"SNAPSHOT-100"))
        self.assertLess(output.index(b"SNAPSHOT-100"), output.rindex(b"LIVE-VIEW"))
        self.assertEqual(output.count(b"\x1b[22;2t"), 1)
        self.assertEqual(output.count(b"\x1b[23;2t"), 1)
        phases = [*self.phases, {"columns": 100, "output": self.output.decode()}]
        proof = subprocess.run(
            ["node", "-e", TERMINAL_PROOF],
            cwd=ROOT,
            input=json.dumps(phases),
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        terminal = json.loads(proof.stdout)
        lines = terminal["lines"]
        self.assertFalse(
            any(
                "UNRELATED-PREATTACH" in line or "SNAPSHOT-80" in line for line in lines
            )
        )
        markers = [line.strip() for line in lines if "HISTORY-" in line]
        self.assertEqual(markers, [f"HISTORY-{i:04d}" for i in range(1, 201)])
        self.assertTrue(
            any("LIVE-VIEW" in line for line in lines[terminal["liveViewportStart"] :])
        )
        self.assertEqual(
            terminal["titles"],
            ["previous-title", "actual-project", "actual-project", "previous-title"],
        )

    def test_resize_burst_fetches_only_latest_dimensions(self):
        self.start_attachment()
        self.resize(90)
        self.drain(0.04)
        self.resize(100)
        self.wait_event("mosh", 2)
        events = self.detach()
        self.assertEqual(self.process.returncode, 0, self.combined_output())
        self.assertEqual(
            [event["columns"] for event in events if event["action"] == "history"],
            [80, 100],
        )
        self.assertEqual(
            [event["columns"] for event in events if event["action"] == "mosh"],
            [80, 100],
        )

    def test_same_size_signal_does_not_replace_mosh(self):
        self.start_attachment()
        self.resize(80)
        self.drain(0.4)
        events = self.detach()
        self.assertEqual(self.process.returncode, 0, self.combined_output())
        self.assertEqual(
            [event["action"] for event in events],
            ["history", "mosh", "mosh-exit", "status"],
        )
        self.assertNotIn(b"\x1b[3J", self.combined_output())

    def test_natural_detach_wins_over_pending_resize(self):
        self.start_attachment()
        self.resize(100)
        events = self.detach()
        self.assertEqual(self.process.returncode, 0, self.combined_output())
        self.assertEqual(
            [event["action"] for event in events],
            ["history", "mosh", "mosh-exit", "status"],
        )
        self.assertEqual(events[2]["reason"], "detach")
        self.assertNotIn(b"\x1b[3J", self.combined_output())

    def test_unavailable_initial_history_keeps_ordinary_mosh(self):
        self.start_attachment(HISTORY_MODE="unavailable")
        self.resize(100)
        self.drain(0.4)
        events = self.detach()
        self.assertEqual(self.process.returncode, 0, self.combined_output())
        self.assertEqual(
            [event["action"] for event in events],
            ["history", "mosh", "mosh-exit", "status"],
        )
        self.assertNotIn(b"\x1b[3J", self.combined_output())

    def assert_failed_refresh(self, mode, message):
        self.start_attachment(REFRESH_MODE=mode)
        self.resize(100)
        self.wait_event("history", 2)
        events = self.finish()
        self.assertNotEqual(self.process.returncode, 0, self.combined_output())
        self.assertEqual(
            [event["action"] for event in events],
            ["history", "mosh", "mosh-exit", "history"],
        )
        output = self.combined_output()
        self.assertIn(message, output)
        self.assertNotIn(b"\x1b[3J", output)
        self.assertNotIn(b"connection lost", output)
        self.assertEqual(output.count(b"\x1b[23;2t"), 1)

    def test_unavailable_resize_history_stops_without_clearing(self):
        self.assert_failed_refresh("unavailable", b"No saved conversation history yet.")

    def test_resize_history_error_stops_without_clearing(self):
        self.assert_failed_refresh("error", b"refresh export failed")

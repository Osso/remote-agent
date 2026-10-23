"""Native localhost Mosh + Rust detach + Pi TUI resize regression.

Requires node (native TypeScript support), @xterm/headless via NODE_PATH,
PI_TUI_SOURCE=packages/tui/src, DETACH=/path/to/Rust/detach, and mosh-client/server.
MOSH_RESIZE_CONTROLLER optionally selects an older controller for regression RED.
Only disposable local sessions are created; no SSH connection or model is used.
"""

import ctypes
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/mosh-resize.ts"
STAGES = [("initial", 70, 16), ("shrink", 40, 8), ("grow", 70, 16)]


def read_events(root):
    events = root / "transport.jsonl"
    return (
        [json.loads(line) for line in events.read_text().splitlines()]
        if events.exists()
        else []
    )


def record_event(root, **event):
    with (root / "transport.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")


def is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def export_over_fake_ssh():
    root = Path(os.environ["MOSH_RESIZE_ROOT"])
    request = json.load(sys.stdin)
    clients = [
        event["pid"] for event in read_events(root) if event["action"] == "client"
    ]
    record_event(root, **request, previous_client_alive=any(map(is_alive, clients)))
    if request["action"] == "history":
        snapshot = subprocess.run(
            ["node", str(FIXTURE), "export", str(request["columns"])],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        print(
            json.dumps(
                {
                    "available": True,
                    "history": snapshot.stdout,
                    "project": "native-isolated",
                }
            )
        )
    elif request["action"] == "status":
        print(json.dumps({"session": "demo", "dead": False}))
    else:
        raise RuntimeError(f"unexpected mutation request: {request['action']}")


def exec_native_mosh():
    """Replace only Mosh's SSH bootstrap; transport and detach are native."""
    root = Path(os.environ["MOSH_RESIZE_ROOT"])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [
            "mosh-server",
            "new",
            "-s",
            "-i",
            "127.0.0.1",
            "-p",
            str(port),
            "--",
            os.environ["DETACH"],
            "-a",
            str(root / "session.sock"),
            "-r",
            "winch",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    record_event(root, action="server", pid=server.pid)
    handshake = bytearray()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if select.select([server.stdout], [], [], 0.05)[0]:
            chunk = os.read(server.stdout.fileno(), 65536)
            if not chunk:
                break
            handshake.extend(chunk)
        match = re.search(rb"MOSH CONNECT (\d+) (\S+)", handshake)
        if match:
            columns, rows = os.get_terminal_size(0)
            record_event(
                root,
                action="client",
                pid=os.getpid(),
                sid=os.getsid(0),
                columns=columns,
                rows=rows,
            )
            os.environ.update(
                MOSH_KEY=match[2].decode(),
                MOSH_NO_TERM_INIT="1",
                MOSH_PREDICTION_DISPLAY="never",
            )
            os.execvp("mosh-client", ["mosh-client", "127.0.0.1", match[1].decode()])
    # Never include the handshake: it contains the connection secret.
    raise RuntimeError(
        "isolated mosh-server did not provide a handshake within five seconds"
    )


def set_size(fd, columns, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


class NativeMoshResizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise unittest.SkipTest(
                "native Mosh cleanup requires Linux /proc and child subreaping"
            )
        for executable in ("node", "mosh-client", "mosh-server"):
            if not shutil.which(executable):
                raise unittest.SkipTest(
                    f"native resize dependency absent: {executable}"
                )
        detach = shutil.which(os.environ.get("DETACH", "detach"))
        if not detach:
            raise unittest.SkipTest(
                "native Rust detach absent; set DETACH to its executable"
            )
        source = os.environ.get("PI_TUI_SOURCE")
        if not source or not (Path(source) / "tui.ts").is_file():
            raise unittest.SkipTest("set PI_TUI_SOURCE to Pi packages/tui/src")
        modules = subprocess.run(
            ["node", "-e", "require('@xterm/headless')"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if modules.returncode:
            raise unittest.SkipTest("@xterm/headless absent; configure NODE_PATH")
        version = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True, timeout=5
        )
        major, minor = map(int, version.stdout.strip().lstrip("v").split(".")[:2])
        if (major, minor) < (22, 18):
            raise unittest.SkipTest("native TypeScript fixture requires Node >=22.18")
        cls.detach = detach

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ra-mosh-resize-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.master = None
        self.controller = None
        self.daemon = None
        self.owned = set()
        self.reaped = set()
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.previous_subreaper = ctypes.c_int()
        self.assertEqual(
            self.libc.prctl(37, ctypes.byref(self.previous_subreaper), 0, 0, 0), 0
        )
        self.assertEqual(self.libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(self.stop_owned_processes)
        self.configure_isolated_home()

    def configure_isolated_home(self):
        config = self.root / ".config/remote-agent"
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
                    },
                }
            )
        )
        (config / "remote-projects.json").write_text("{}")
        bindir = self.root / "bin"
        bindir.mkdir()
        for executable in ("ssh", "mosh"):
            wrapper = bindir / executable
            wrapper.write_text(
                f"#!{sys.executable}\nimport runpy, sys\n"
                f"sys.argv = [{str(Path(__file__).resolve())!r}, '--{executable}']\n"
                f"runpy.run_path(sys.argv[0], run_name='__main__')\n"
            )
            wrapper.chmod(0o700)
        self.env = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "DETACH": self.detach,
            "MOSH_RESIZE_ROOT": str(self.root),
            "PI_TUI_SOURCE": str(Path(os.environ["PI_TUI_SOURCE"]).resolve()),
        }

    def start_attachment(self):
        session = self.root / "session.sock"
        self.daemon = subprocess.Popen(
            [
                self.detach,
                "-N",
                str(session),
                "-r",
                "winch",
                "node",
                str(FIXTURE),
                "tui",
                str(self.root / "input.jsonl"),
            ],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.owned.add(self.daemon.pid)
        deadline = time.monotonic() + 5
        while not session.exists() and time.monotonic() < deadline:
            self.assertIsNone(self.daemon.poll(), "native detach failed to start")
            time.sleep(0.02)
        self.assertTrue(session.exists(), "native detach socket missing")
        controller = Path(
            os.environ.get("MOSH_RESIZE_CONTROLLER", ROOT / "remote-agent.py")
        ).resolve()
        pid, self.master = pty.fork()
        if pid == 0:
            set_size(1, 70, 16)
            os.execve(
                sys.executable,
                [sys.executable, str(controller), "attach", "demo"],
                self.env,
            )
        self.controller = pid
        self.owned.add(pid)

    def drain(self, duration):
        output = bytearray()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select(
                [self.master], [], [], min(0.03, max(0, deadline - time.monotonic()))
            )[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
        return bytes(output)

    def capture_stage(self, index, name, columns, rows):
        if index:
            set_size(self.master, columns, rows)
        output = bytearray()
        started = time.monotonic()
        while time.monotonic() - started < 12:
            output.extend(self.drain(0.1))
            clients = [
                event for event in read_events(self.root) if event["action"] == "client"
            ]
            if len(clients) >= index + 1 and time.monotonic() - started > 2:
                break
        self.assertEqual(
            len(clients),
            index + 1,
            f"{name}: native Mosh failed to attach; events={read_events(self.root)}",
        )
        output.extend(self.drain(1))
        if index == 0:
            os.write(self.master, b"n")
        output.extend(self.drain(1))
        return {
            "name": name,
            "columns": columns,
            "rows": rows,
            "output": output.decode(errors="replace"),
        }

    def assert_terminal_history(self, stages):
        analysis = subprocess.run(
            ["node", str(FIXTURE), "analyze"],
            input=json.dumps(stages),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            env=self.env,
        )
        proof = json.loads(analysis.stdout)
        for index, report in enumerate(proof["reports"]):
            with self.subTest(stage=report["name"]):
                history = "\n".join(report["history"])
                visible = "\n".join(report["visible"])
                ids = [int(value) for value in re.findall(r"PROOF_(\d{3})", history)]
                self.assertEqual(
                    ids,
                    list(range(proof["paragraphCount"])),
                    "saved paragraphs must occur exactly once in order",
                )
                pattern = "|".join(map(re.escape, proof["sentinels"]))
                self.assertEqual(re.findall(pattern, history), proof["sentinels"])
                self.assertIn(
                    "LIVE_NEW_REPLY", visible, "new unsaved reply must survive resize"
                )
                self.assertEqual(report["clearScrollback"], index > 0)
                if index:
                    self.assertNotIn("STALE_BEFORE_ATTACH", history + visible)
                # The live viewport may repeat the recent canonical tail.

    def test_native_resize_replaces_saved_history_and_preserves_live_session(self):
        self.start_attachment()
        stages = [
            self.capture_stage(index, *stage) for index, stage in enumerate(STAGES)
        ]
        self.assert_terminal_history(stages)
        self.assertTrue(
            (self.root / "input.jsonl").exists(), "input never reached the real TUI"
        )
        events = read_events(self.root)
        histories = [event for event in events if event["action"] == "history"]
        self.assertEqual(
            [event["columns"] for event in histories],
            [70, 40, 70],
        )
        self.assertFalse(
            any(event["previous_client_alive"] for event in histories),
            "old Mosh must exit before fresh SSH export",
        )
        clients = [event for event in events if event["action"] == "client"]
        self.assertEqual(
            [(event["columns"], event["rows"]) for event in clients],
            [(70, 16), (40, 8), (70, 16)],
        )
        self.assertTrue(all(event["pid"] == event["sid"] for event in clients))
        self.assertEqual(len({event["pid"] for event in clients}), 3)
        os.write(self.master, b"\x1c")
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.controller, os.WNOHANG)
            if pid:
                self.reaped.add(pid)
                break
            self.drain(0.1)
        self.assertIn(
            self.controller, self.reaped, "Ctrl+backslash did not exit controller"
        )
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertIsNone(self.daemon.poll(), "detach killed the persistent session")
        fixture_pids = [
            json.loads(line)["pid"]
            for line in (self.root / "input.jsonl").read_text().splitlines()
        ]
        self.assertTrue(
            all(map(is_alive, fixture_pids)),
            "real Pi TUI fixture did not survive detach",
        )
        self.assertTrue((self.root / "session.sock").exists())
        self.assertNotIn(
            "connection lost", "".join(stage["output"] for stage in stages).lower()
        )

    def stop_owned_processes(self):
        self.owned.update(
            event["pid"]
            for event in read_events(self.root)
            if event["action"] in ("client", "server")
        )
        groups = set(self.owned)
        # Include native attach descendants in owned groups, including on test failure.
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                try:
                    if os.getpgid(int(entry.name)) in groups:
                        self.owned.add(int(entry.name))
                except ProcessLookupError:
                    pass
        try:
            for group in groups:
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and any(map(is_alive, self.owned)):
                for pid in self.owned - self.reaped:
                    try:
                        reaped, _ = os.waitpid(pid, os.WNOHANG)
                        if reaped:
                            self.reaped.add(reaped)
                    except ChildProcessError:
                        pass
                time.sleep(0.02)
            survivors = [pid for pid in self.owned if is_alive(pid)]
            for pid in survivors:
                os.kill(pid, signal.SIGKILL)
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            if self.daemon:
                self.daemon.wait(timeout=3)
            self.assertEqual(survivors, [], "native fixture required forced cleanup")
            self.assertFalse(
                any(map(is_alive, self.owned)), "owned native process leaked"
            )
            self.assertEqual(list(self.root.glob("*.sock")), [], "native socket leaked")
        finally:
            if self.master is not None:
                os.close(self.master)
            self.libc.prctl(36, self.previous_subreaper.value, 0, 0, 0)


if __name__ == "__main__":
    if sys.argv[1:] == ["--ssh"]:
        export_over_fake_ssh()
    elif sys.argv[1:] == ["--mosh"]:
        exec_native_mosh()
    else:
        unittest.main()

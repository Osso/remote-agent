"""Native CLI -> QUIC -> session PTY -> real Pi bulk-output regression.

Run directly with Python on Linux. REMOTE_AGENT_RUST_BIN overrides target/debug/
remote-agent-rs. NODE_PATH and PI_TUI_SOURCE may override the local Pi checkout.
SSH and systemd-manager bootstrap are substituted; PTYs, QUIC, Rust processes,
and the Pi TUI are native. No real account, provider, or model is used.
Evidence, including failed runs, remains in /var/tmp/pi-resize-fix/rust-cli/.
"""

import ctypes
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shlex
import signal
import shutil
import socket
import stat
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
EVIDENCE = Path("/var/tmp/pi-resize-fix/rust-cli")


def ssh_bootstrap():
    """Execute the real JSON control server, without invoking a remote shell."""
    arguments = sys.argv[2:]
    root = Path(os.environ["RUST_ATTACH_HOME"])
    with (root / "ssh-events.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": arguments, "pid": os.getpid()}) + "\n")
    while arguments[:1] == ["-o"]:
        if len(arguments) < 2:
            raise RuntimeError("SSH option is missing its value")
        arguments = arguments[2:]
    if arguments == ["-G", "fixture-host"]:
        print("hostname 127.0.0.1")
        return
    if len(arguments) != 2 or arguments[0] != "fixture-host":
        raise RuntimeError(f"unexpected SSH bootstrap arguments: {arguments!r}")
    command = shlex.split(arguments[1])
    expected = [str(root / ".local/bin/remote-agent-rs"), "__rpc"]
    if command != expected:
        raise RuntimeError(f"unexpected remote command: {command!r}")
    if (root / "ssh-offline").exists():
        print("fixture SSH control transport is unavailable", file=sys.stderr)
        sys.exit(255)
    os.environ["HOME"] = str(root)
    os.execv(command[0], command)


UNIT = re.compile(r"remote-agent-rs@([A-Za-z0-9][A-Za-z0-9_-]*)\.service")


def unit_fixture_state(unit):
    match = UNIT.fullmatch(unit)
    if not match:
        raise RuntimeError(f"unexpected session unit: {unit}")
    return (
        Path(os.environ["RUST_ATTACH_HOME"])
        / ".local/state/remote-agent-rs/sessions"
        / match[1]
    )


def start_unit_fixture(state):
    """Run the server as the installed remote-agent-rs@.service template does."""
    home = Path(os.environ["RUST_ATTACH_HOME"])
    command = [home / ".local/bin/remote-agent-rs", "__serve", "--state", state]
    with (state / "server.log").open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={**os.environ, "HOME": str(home)},
        )
    (state / "fixture-unit.json").write_text(
        json.dumps({"enabled": True, "pid": process.pid})
    )


def systemctl_bootstrap():
    """User-manager substitute for enable/disable --now and boot-time start."""
    arguments = sys.argv[2:]
    if len(arguments) not in (3, 4) or arguments[0] != "--user":
        raise RuntimeError(f"unexpected fixture manager call: {arguments}")
    action, unit = arguments[1], arguments[-1]
    state = unit_fixture_state(unit)
    path = state / "fixture-unit.json"
    info = json.loads(path.read_text()) if path.exists() else None
    if arguments[1:3] == ["enable", "--now"]:
        start_unit_fixture(state)
    elif action == "start" and info and info["enabled"]:
        start_unit_fixture(state)
    elif action == "restart" and info and info["enabled"]:
        if (state / "fixture-restart-failure").exists():
            print("fixture manager restart denied", file=sys.stderr)
            sys.exit(1)
        runtime = json.loads((state / "runtime.json").read_text())
        for pid in (runtime["child_pid"], info["pid"]):
            if pid and process_exists(pid):
                os.kill(pid, signal.SIGKILL)
        start_unit_fixture(state)
    elif arguments[1:3] == ["disable", "--now"]:
        if info and process_exists(info["pid"]):
            command = Path(f"/proc/{info['pid']}/cmdline").read_bytes().split(b"\0")
            # An exited, not yet reaped server has an empty command line.
            if command[0]:
                if command[1:4] != [b"__serve", b"--state", os.fsencode(state)]:
                    raise RuntimeError("refusing to stop an unrelated fixture process")
                os.kill(info["pid"], signal.SIGTERM)
        if info:
            path.write_text(json.dumps({**info, "enabled": False}))
    else:
        raise RuntimeError(f"unexpected fixture manager call: {arguments}")


def process_exists(pid):
    return Path(f"/proc/{pid}").exists()


def children():
    path = Path(f"/proc/self/task/{os.getpid()}/children")
    return {int(value) for value in path.read_text().split()}


class NativeRustAttachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise unittest.SkipTest(
                "native session identity and cleanup require Linux /proc"
            )
        cls.binary = Path(
            os.environ.get(
                "REMOTE_AGENT_RUST_BIN", ROOT / "target/debug/remote-agent-rs"
            )
        ).resolve()
        if not cls.binary.is_file() or not os.access(cls.binary, os.X_OK):
            raise unittest.SkipTest(
                f"native Rust binary absent: {cls.binary}; set REMOTE_AGENT_RUST_BIN"
            )
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("native Pi fixture requires node")
        cls.source = Path(
            os.environ.get("PI_TUI_SOURCE", "/home/osso/Repos/pi/packages/tui/src")
        ).resolve()
        if not (cls.source / "tui.ts").is_file():
            raise unittest.SkipTest(
                f"Pi TUI source absent: {cls.source}; set PI_TUI_SOURCE"
            )
        cls.fixture_env = {
            **os.environ,
            "NODE_PATH": os.environ.get(
                "NODE_PATH", "/home/osso/Repos/pi/node_modules"
            ),
            "PI_TUI_SOURCE": str(cls.source),
        }
        probe = subprocess.run(
            [
                cls.node,
                "-e",
                "require('@xterm/headless'); const [major,minor]=process.versions.node.split('.').map(Number); if(major<22 || (major===22 && minor<18))process.exit(1)",
            ],
            env=cls.fixture_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode:
            raise unittest.SkipTest(
                f"Node >=22.18 with @xterm/headless required: {probe.stderr}"
            )

    def setUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="run-", dir=EVIDENCE))
        self.state = self.root / ".local/state/remote-agent-rs/sessions/bulk-proof"
        self.stages = []
        self.master = None
        self.client = None
        self.reaped = set()
        self.owned = set()
        self.baseline_children = children()
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.previous_subreaper = ctypes.c_int()
        self.assertEqual(
            self.libc.prctl(37, ctypes.byref(self.previous_subreaper), 0, 0, 0), 0
        )
        self.assertEqual(self.libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(self.cleanup_session)
        self.env = {
            **self.fixture_env,
            "HOME": str(self.root),
            "XDG_CONFIG_HOME": str(self.root / ".config"),
            "XDG_STATE_HOME": str(self.root / ".local/state"),
            "RUST_ATTACH_HOME": str(self.root),
            "TERM": "xterm-256color",
            "TERM_PROGRAM": "ghostty",
            "GHOSTTY_RESOURCES_DIR": "/fixture/physical-terminal",
            "LANG": "C.UTF-8",
        }
        self.env.pop("REMOTE_AGENT_AGENT_SERVER_SSH", None)
        self.configure_home()
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.record(
            "scope",
            revision=revision,
            binary=str(self.binary),
            sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            command=[sys.executable, str(Path(__file__).resolve())],
        )
        print(f"Native Rust evidence: {self.root}", flush=True)

    def record(self, action, **fields):
        with (self.root / "ledger.jsonl").open("a") as stream:
            stream.write(json.dumps({"action": action, **fields}) + "\n")

    def configure_home(self):
        config = self.root / ".config/remote-agent"
        config.mkdir(parents=True)
        bindir = self.root / ".local/bin"
        bindir.mkdir(parents=True)
        (bindir / "remote-agent-rs").symlink_to(self.binary)
        project = self.root / "project"
        project.mkdir()
        wrapper = bindir / "pi-fixture"
        # Records the Pi arguments of every launch, then runs the TUI fixture.
        wrapper.write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            f"open({str(self.root / 'pi-argv.jsonl')!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
            f"os.execv({self.node!r}, [{self.node!r}, {str(FIXTURE)!r}, 'bulk-tui', {str(self.root / 'input.jsonl')!r}])\n"
        )
        wrapper.chmod(0o700)
        ssh = bindir / "ssh"
        ssh.write_text(
            f"#!{sys.executable}\nimport runpy, sys\n"
            f"sys.argv = [{str(Path(__file__).resolve())!r}, '--ssh', *sys.argv[1:]]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        ssh.chmod(0o700)
        program = bindir / "systemctl"
        program.write_text(
            f"#!{sys.executable}\nimport runpy,sys\n"
            f"sys.argv = [{str(Path(__file__).resolve())!r}, '--systemctl', *sys.argv[1:]]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        program.chmod(0o700)
        self.env["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
        (config / "remote-hosts.json").write_text(
            json.dumps(
                {
                    "agent-server": {
                        "ssh": "fixture-host",
                        "home": str(self.root),
                        "wrap": [],
                        "windows_shell": False,
                        "cargo_build_jobs": "2",
                    },
                }
            )
        )
        (config / "remote-projects.json").write_text(
            json.dumps(
                {
                    "demo": {"remote": str(project), "pi_executable": str(wrapper)},
                }
            )
        )

    def cli(self, *arguments, check=True):
        result = subprocess.run(
            [str(self.binary), *arguments],
            env=self.env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.record(
            "cli",
            argv=list(arguments),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if check:
            self.assertEqual(
                result.returncode,
                0,
                f"CLI {arguments}: {result.stderr}\n{result.stdout}\n{self.server_log()}",
            )
            return json.loads(result.stdout)
        return result

    def server_log(self):
        path = self.state / "server.log"
        return path.read_text() if path.exists() else "no server.log"

    def events(self):
        path = self.root / "input.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def drain(self, duration):
        output = bytearray()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select(
                [self.master], [], [], min(0.03, max(0, deadline - time.monotonic()))
            )[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output.extend(data)
        return bytes(output)

    def attach(self):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 46, 165, 0, 0))
        self.original_termios = termios.tcgetattr(slave)
        pid = os.fork()
        if pid == 0:
            os.close(self.master)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for fd in (0, 1, 2):
                os.dup2(slave, fd)
            if slave > 2:
                os.close(slave)
            os.execve(
                str(self.binary), [str(self.binary), "attach", "bulk-proof"], self.env
            )
        os.close(slave)
        self.client = pid
        self.owned.add(pid)
        self.record("attach", pid=pid)

    def capture(self, phase, *, wait_phase=True):
        output = bytearray()
        deadline = time.monotonic() + 12
        try:
            while time.monotonic() < deadline:
                output.extend(self.drain(0.1))
                phase_ready = not wait_phase or any(
                    event["phase"] == phase for event in self.events()
                )
                if "unsupported" in self.server_log().lower():
                    self.fail(f"Pi terminal startup rejected: {self.server_log()}")
                pid, status = os.waitpid(self.client, os.WNOHANG)
                if pid:
                    self.reaped.add(pid)
                    self.client = None
                    self.fail(
                        f"client exited {os.waitstatus_to_exitcode(status)} before {phase}: {output.decode(errors='replace')}"
                    )
                snapshot_rendered = (
                    b"\x1b[3J" in output
                    if phase in ("ready", "reconnected")
                    else bool(output)
                )
                if phase_ready and snapshot_rendered:
                    output.extend(self.drain(1))
                    return
            self.fail(
                f"phase {phase!r} timed out: {self.server_log()}; client={output.decode(errors='replace')}"
            )
        finally:
            stage = {"name": phase, "output": output.decode(errors="replace")}
            self.stages.append(stage)
            (self.root / f"{len(self.stages):02d}-{phase}.ansi").write_bytes(output)
            (self.root / "stages.json").write_text(json.dumps(self.stages))

    def detach(self):
        os.write(self.master, b"\x1c")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.client, os.WNOHANG)
            if pid:
                self.reaped.add(pid)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                self.assertEqual(
                    termios.tcgetattr(self.master),
                    self.original_termios,
                    "Ctrl-backslash must restore the original PTY attributes",
                )
                os.close(self.master)
                self.master = None
                self.client = None
                return
            self.drain(0.05)
        self.fail("Ctrl-backslash did not detach the native CLI")

    def analyze(self, stages, name):
        result = subprocess.run(
            [self.node, str(FIXTURE), "analyze-live"],
            input=json.dumps(stages),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        (self.root / f"{name}-terminal.json").write_text(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_bulk(self, report, phase):
        history = "\n".join(report["history"])
        visible = "\n".join(report["visible"])
        transcript = history + "\n" + visible
        widgets = phase in ("widgets", "partial")
        expected = [f"BULK_LINE_{index:03d}" for index in range(80)]
        if widgets:
            expected += ["TOOL_WIDGET", "EVENT_WIDGET"]
        self.assertEqual(
            re.findall(r"BULK_LINE_\d{3}|TOOL_WIDGET|EVENT_WIDGET", transcript),
            expected,
            f"{phase}: all 80 lines and widgets must occur once in order in terminal history + viewport",
        )
        self.assertIn("BULK_LINE_000", history)
        self.assertIn("EVENT_WIDGET" if widgets else "BULK_LINE_079", visible)
        if phase != "bulk":
            self.assertIn("TABLE_EXPANDED", transcript)
            self.assertNotIn("TABLE_PENDING", transcript)
        else:
            self.assertIn("TABLE_PENDING", transcript)
        if phase == "partial":
            self.assertIn("TOOL_WIDGET complete", visible)
            self.assertNotIn("TOOL_WIDGET pending", transcript)

    def test_bulk_output_and_reconnect_preserve_real_pi_history(self):
        started = self.cli("start", "demo", "bulk-proof")
        self.assertTrue(started["running"], self.server_log())
        runtime = json.loads((self.state / "runtime.json").read_text())
        self.owned.update((runtime["pid"], runtime["child_pid"]))
        self.assertEqual(started["pid"], runtime["pid"])
        self.assertEqual(started["child_pid"], runtime["child_pid"])
        self.assertEqual(
            Path(f"/proc/{runtime['child_pid']}/cwd").resolve(), self.root / "project"
        )
        server_args = Path(f"/proc/{runtime['pid']}/cmdline").read_bytes().split(b"\0")
        self.assertEqual(
            server_args[1:4], [b"__serve", b"--state", os.fsencode(self.state)]
        )
        self.assertTrue(os.path.samefile(f"/proc/{runtime['pid']}/exe", self.binary))
        self.attach()
        self.capture("ready")
        self.assertEqual(
            {event["pid"] for event in self.events()}, {runtime["child_pid"]}
        )
        for key, phase in (
            (b"b", "bulk"),
            (b"r", "revised"),
            (b"w", "widgets"),
            (b"p", "partial"),
        ):
            os.write(self.master, key)
            self.capture(phase)
        reports = self.analyze(self.stages, "live")
        for report in reports[1:]:
            self.assert_bulk(report, report["name"])
        logs = subprocess.run(
            [str(self.binary), "logs", "bulk-proof"],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout
        self.assertNotIn("\x1b", logs, "logs must not repaint the caller's terminal")
        self.assertEqual(
            re.findall(r"BULK_LINE_(\d{3})", logs),
            [f"{index:03}" for index in range(80)],
        )
        self.assertIn("TOOL_WIDGET complete", logs)
        self.detach()
        detached = self.cli("status", "bulk-proof")
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(detached[field], started[field])
        self.assertTrue(detached["running"])
        self.attach()
        self.capture("reconnected", wait_phase=False)
        self.assert_bulk(self.analyze([self.stages[-1]], "reconnected")[0], "partial")
        reconnected = self.cli("status", "bulk-proof")
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(reconnected[field], started[field])
        self.assertEqual(
            {event["pid"] for event in self.events()}, {runtime["child_pid"]}
        )
        self.detach()
        self.record(
            "behavior", result="passed", phases=[stage["name"] for stage in self.stages]
        )

    def test_unavailable_ssh_bootstrap_preserves_terminal_and_server(self):
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        offline = self.root / "ssh-offline"
        offline.touch()
        try:
            self.attach()
            output = bytearray()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                output.extend(self.drain(0.05))
                pid, status = os.waitpid(self.client, os.WNOHANG)
                if pid:
                    self.reaped.add(pid)
                    self.client = None
                    self.assertEqual(os.waitstatus_to_exitcode(status), 1)
                    break
            else:
                self.fail("unavailable initial SSH bootstrap did not fail promptly")
            self.assertIn(b"fixture SSH control transport is unavailable", output)
            self.assertEqual(termios.tcgetattr(self.master), self.original_termios)
            os.close(self.master)
            self.master = None
        finally:
            offline.unlink()
        after = self.cli("status", "bulk-proof")
        for field in ("pid", "child_pid", "conversation"):
            self.assertEqual(after[field], started[field])
        self.assertTrue(after["running"])

    def configure_cancel_hook(self, fail_once=False):
        hook = self.root / "cancel-hook"
        hook.write_text(
            f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
            f"root=Path({str(self.root)!r})\nstate=Path({str(self.state)!r})\n"
            "runtime=json.loads((state/'runtime.json').read_text())\n"
            "path=Path('/proc/'+str(runtime['pid'])+'/stat')\n"
            "alive=path.exists() and path.read_text().rsplit(')',1)[1].split()[0] not in ('Z','X')\n"
            "with (root/'hook-calls.jsonl').open('a') as stream: stream.write(json.dumps({'args':sys.argv[1:],'server_alive':alive})+'\\n')\n"
            "failure=root/'hook-fail-once'\n"
            "if failure.exists():\n failure.unlink()\n print('fixture teardown rejected',file=sys.stderr)\n sys.exit(1)\n"
            "print('fixture teardown complete')\n"
        )
        hook.chmod(0o700)
        path = self.root / ".config/remote-agent/remote-projects.json"
        projects = json.loads(path.read_text())
        projects["demo"]["pi_cancel_executable"] = str(hook)
        path.write_text(json.dumps(projects))
        if fail_once:
            (self.root / "hook-fail-once").touch()

    def test_cancel_hook_runs_after_unit_stop_and_can_retry(self):
        self.configure_cancel_hook(fail_once=True)
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        failed = self.cli("cancel", "bulk-proof", check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("fixture teardown rejected", failed.stderr)
        self.assertTrue((self.state / "launch.json").exists())
        succeeded = self.cli("cancel", "bulk-proof")
        self.assertEqual(succeeded["cancelled"], "bulk-proof")
        calls = [
            json.loads(line)
            for line in (self.root / "hook-calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(
                call["args"] == [started["conversation"]] and not call["server_alive"]
                for call in calls
            )
        )

    def test_cancel_refuses_unrelated_pid_before_unit_stop_or_hook(self):
        self.configure_cancel_hook()
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        path = self.state / "runtime.json"
        original = path.read_text()
        altered = json.loads(original)
        altered["pid"] = os.getpid()
        try:
            path.write_text(json.dumps(altered))
            result = self.cli("cancel", "bulk-proof", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not own", result.stderr)
            self.assertFalse((self.root / "hook-calls.jsonl").exists())
            self.assertTrue(process_exists(started["pid"]))
        finally:
            path.write_text(original)

    def test_restart_resumes_named_conversation(self):
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        argv_log = self.root / "pi-argv.jsonl"
        deadline = time.monotonic() + 5
        while not argv_log.exists() or not argv_log.read_text().endswith("\n"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        result = self.cli("restart", "bulk-proof")
        self.owned.update((result["pid"], result["child_pid"]))
        self.assertTrue(result["running"])
        self.assertNotEqual(result["pid"], started["pid"])
        self.assertEqual(result["conversation"], started["conversation"])
        self.assertEqual(result["cwd"], started["cwd"])
        deadline = time.monotonic() + 5
        while not argv_log.exists() or len(argv_log.read_text().splitlines()) < 2:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        argv = json.loads(argv_log.read_text().splitlines()[-1])
        self.assertEqual(argv[:2], ["--session", started["conversation"]])

    def test_restart_refuses_unrelated_live_server_pid(self):
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        path = self.state / "runtime.json"
        original = path.read_bytes()
        try:
            runtime = json.loads(original)
            runtime["pid"] = os.getpid()
            path.write_text(json.dumps(runtime))
            result = self.cli("restart", "bulk-proof", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not own this session", result.stderr)
            self.assertTrue(process_exists(started["pid"]))
        finally:
            path.write_bytes(original)

    def test_restart_reports_service_failure(self):
        started = self.cli("start", "demo", "bulk-proof")
        self.owned.update((started["pid"], started["child_pid"]))
        (self.state / "fixture-restart-failure").touch()
        result = self.cli("restart", "bulk-proof", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fixture manager restart denied", result.stderr)
        self.assertEqual(self.cli("status", "bulk-proof")["pid"], started["pid"])

    def test_enabled_session_unit_restores_after_simulated_reboot(self):
        # Verifier counterexample: a prompted session failed to restart (prompt.txt
        # EEXIST); restarting must also never submit the initial prompt again.
        prompt = self.root / "initial-prompt.txt"
        prompt.write_text("fixture prompt; no provider request\n")
        started = self.cli("start", "demo", "bulk-proof", "--prompt-file", str(prompt))
        self.owned.update((started["pid"], started["child_pid"]))
        launch = (self.state / "launch.json").read_bytes()
        # The fixture records argv after exec; killing earlier would lose the first launch.
        argv_log = self.root / "pi-argv.jsonl"
        deadline = time.monotonic() + 5
        while not (argv_log.exists() and argv_log.read_text().endswith("\n")):
            self.assertLess(time.monotonic(), deadline, "first Pi launch not recorded")
            time.sleep(0.02)
        # Power loss: the server and Pi die without cancel; the unit stays enabled.
        for pid in (started["child_pid"], started["pid"]):
            os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(
            process_exists(pid) for pid in (started["pid"], started["child_pid"])
        ):
            self.assertLess(time.monotonic(), deadline, "killed session survived")
            for pid in (started["pid"], started["child_pid"]):
                try:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        self.reaped.add(pid)
                except ChildProcessError:
                    pass
            time.sleep(0.02)
        self.assertFalse(self.cli("status", "bulk-proof")["running"])
        # Boot: the user manager starts every enabled session unit.
        subprocess.run(
            [
                self.root / ".local/bin/systemctl",
                "--user",
                "start",
                "remote-agent-rs@bulk-proof.service",
            ],
            env=self.env,
            check=True,
            timeout=10,
        )
        deadline = time.monotonic() + 10
        while True:
            restored = self.cli("status", "bulk-proof")
            if restored["running"] and restored["pid"] != started["pid"]:
                break
            self.assertLess(time.monotonic(), deadline, "session did not restart")
            time.sleep(0.05)
        self.owned.update((restored["pid"], restored["child_pid"]))
        launches = [
            json.loads(line)
            for line in (self.root / "pi-argv.jsonl").read_text().splitlines()
        ]
        prompt_argument = "@" + str(self.state / "prompt.txt")
        self.assertEqual(
            launches,
            [
                ["--session-id", started["conversation"], prompt_argument],
                ["--session-id", started["conversation"]],
            ],
        )
        self.assertEqual(restored["conversation"], started["conversation"])
        self.assertEqual(restored["project"], started["project"])
        self.assertEqual((self.state / "launch.json").read_bytes(), launch)
        self.assertIsNone(json.loads(launch)["prompt"])
        self.attach()
        self.capture("ready")
        os.write(self.master, b"b")
        self.capture("bulk")
        self.assert_bulk(self.analyze(self.stages, "restored")[-1], "bulk")
        self.detach()

    def cleanup_session(self):
        errors = []
        try:
            if self.client and process_exists(self.client):
                try:
                    self.detach()
                except (AssertionError, OSError, ChildProcessError) as error:
                    errors.append(str(error))
            if (self.state / "runtime.json").exists():
                result = self.cli("cancel", "bulk-proof", check=False)
                if result.returncode and "refusing to signal" not in result.stderr:
                    errors.append(f"CLI cancellation failed: {result.stderr}")
            # A failed server can leave the client in its bounded reconnect loop.
            # Allow that loop to exit without signalling outside the CLI.
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                self.owned.update(children() - self.baseline_children)
                for pid in self.owned - self.reaped:
                    try:
                        reaped, _ = os.waitpid(pid, os.WNOHANG)
                        if reaped:
                            self.reaped.add(reaped)
                    except ChildProcessError:
                        pass
                if not any(process_exists(pid) for pid in self.owned):
                    break
                time.sleep(0.05)
            survivors = [pid for pid in self.owned if process_exists(pid)]
            sockets = [
                str(path)
                for path in self.root.rglob("*")
                if stat.S_ISSOCK(path.lstat().st_mode)
            ]
            self.record("cleanup", survivors=survivors, sockets=sockets, errors=errors)
            self.assertEqual(
                survivors, [], "owned native process leaked after actual CLI cancel"
            )
            self.assertEqual(sockets, [], "owned session socket leaked")
            runtime_path = self.state / "runtime.json"
            if runtime_path.exists():
                runtime = json.loads(runtime_path.read_text())
                port = int(runtime["address"].rsplit(":", 1)[1])
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.bind(("0.0.0.0", port))
                self.record("quic-socket-released", port=port)
            self.assertEqual(
                children() - self.baseline_children, set(), "owned children leaked"
            )
            self.assertEqual(errors, [], "native CLI cleanup failed")
        finally:
            if self.master is not None:
                os.close(self.master)
            self.libc.prctl(36, self.previous_subreaper.value, 0, 0, 0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--ssh"]:
        ssh_bootstrap()
    elif sys.argv[1:2] == ["--systemctl"]:
        systemctl_bootstrap()
    else:
        unittest.main()

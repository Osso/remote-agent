"""Real dtach/PTY lifecycle; fixture agents never call providers."""

import gc
import importlib.util
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DTACH = shutil.which("dtach")

FIXTURE = """#!/usr/bin/env python3
import json, os, signal, subprocess, sys, termios, tty
from pathlib import Path
home = Path(os.environ["HOME"])
tty.setraw(0)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with (home / "calls").open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
        "jobs": os.environ.get("CARGO_BUILD_JOBS"),
        "scripts": os.environ.get("BUILD_HOST_SCRIPTS"),
        "pi_dir": os.environ.get("PI_CODING_AGENT_DIR"),
        "runtime": os.environ.get("XDG_RUNTIME_DIR"),
        "bus": os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
        "term": os.environ.get("TERM"), "colorterm": os.environ.get("COLORTERM"),
        "child": child.pid, "pid": os.getpid()}) + "\\n")
signal.signal(signal.SIGWINCH, lambda *_: os.write(1, b"WINCH\\r\\n"))
while True:
    data = os.read(0, 1024)
    with (home / "input").open("ab") as f:
        f.write(data)
    os.write(1, b"ECHO:" + data)
    if b"Q" in data:
        child.terminate()
        child.wait()
        sys.exit(7)
"""


PASTE_FIXTURE = r"""#!/usr/bin/env python3
import json, os, tty
from pathlib import Path

home = Path(os.environ["HOME"])
tty.setraw(0)
state = {"pid": os.getpid(), "pastes": [], "submissions": [], "received": 0}
pending = b""
pasting = False

def save():
    temporary = home / "paste-state.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(home / "paste-state.json")

# Emit once, while detached; SIGWINCH deliberately does not re-enable it.
os.write(1, b"\x1b[?2004hSTARTUP-ONLY\r\n")
save()
while True:
    data = os.read(0, 1)
    if data == b"\x1b":
        while len(data) < 6:
            data += os.read(0, 6 - len(data))
    state["received"] += len(data)
    if data == b"\x1b[200~":
        pasting = True
    elif data == b"\x1b[201~":
        state["pastes"].append(pending.decode())
        pending = b""
        pasting = False
    elif data == b"\r" and not pasting:
        state["submissions"].append(pending.decode())
        pending = b""
    else:
        pending += data
    save()
"""


def wait_for(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("bounded wait expired")


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@unittest.skipUnless(DTACH, "real dtach executable required on PATH")
class DtachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ra-dtach-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True)
        (config / "remote-hosts.json").write_text(
            json.dumps(
                {
                    "desktop": {
                        "cargo_build_jobs": "3",
                        "build_host_scripts": "/fixture/build",
                    }
                }
            )
        )
        (config / "remote-projects.json").write_text(
            json.dumps({"fixture": {"remote": "project"}})
        )
        (self.home / "project").mkdir()
        binaries = self.home / ".local/bin"
        binaries.mkdir(parents=True)
        (binaries / "pi").write_text(FIXTURE)
        (binaries / "pi").chmod(0o700)
        self.env = {**os.environ, "HOME": str(self.home), "TERM": "xterm-256color"}
        self.patch = mock.patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        spec = importlib.util.spec_from_file_location(
            "dtach_worker", ROOT / "remote-agent-worker.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmux = ["tmux", "-L", "dtach-test-" + self.home.name, "-f", "/dev/null"]
        self.worker = self.module.Worker(self.home, self.tmux, scope=[])
        self.addCleanup(self.clean_sessions)

    def clean_sessions(self):
        if not self.worker.state.exists():
            return
        for directory in self.worker.state.iterdir():
            for file in directory.iterdir():
                if not file.is_symlink():
                    file.chmod(0o700 if file.is_socket() else 0o600)
            self.worker.dispatch(
                {
                    "action": "cancel",
                    "name": directory.name.removeprefix("remote-agent-"),
                }
            )

    def call(self, action, name="trial", **kwargs):
        return self.worker.dispatch({"action": action, "name": name, **kwargs})

    def start(self, name="trial", **kwargs):
        return self.call("start", name, repo="fixture", agent="pi", **kwargs)

    def directory(self, name="trial"):
        return self.worker.state / self.module.session_name(name)

    def calls(self):
        path = self.home / "calls"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def attach(self, name="trial", colorterm=None, tty_output=True):
        master, slave = pty.openpty()
        color_args = [] if colorterm is None else [f"--colorterm={colorterm}"]
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "remote-agent-worker.py"),
                *color_args,
                "--attach",
                name,
            ],
            env=self.env,
            stdin=slave,
            stdout=slave if tty_output else subprocess.PIPE,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        if process.stdout is not None:
            self.addCleanup(process.stdout.close)
        self.addCleanup(os.close, master)
        self.addCleanup(process.wait, timeout=3)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        wait_for(
            lambda: (
                self.call("list")["sessions"][0]["attached"]
                or process.poll() is not None
            )
        )
        if process.poll() is not None:
            self.fail(self.read(master))
        return process, master

    @staticmethod
    def read(fd, timeout=0.15):
        output = b""
        while select.select([fd], [], [], timeout)[0]:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            output += data
        return output

    def test_launch_uses_requested_truecolor_with_mosh_terminal(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("TERM", None)
            os.environ.pop("COLORTERM", None)
            self.start(colorterm="truecolor")
        call = wait_for(lambda: self.calls())[0]
        self.assertEqual(call["term"], "xterm-256color")
        self.assertEqual(call["colorterm"], "truecolor")

    def test_launch_without_truecolor_does_not_inherit_server_hint(self):
        with mock.patch.dict(os.environ, {"COLORTERM": "truecolor"}):
            self.start(colorterm="")
        call = wait_for(lambda: self.calls())[0]
        self.assertIsNone(call["colorterm"])

    def test_invalid_color_hint_fails_before_creating_session(self):
        with self.assertRaisesRegex(ValueError, "colorterm"):
            self.start(colorterm="arbitrary-environment")
        self.assertFalse(self.directory().exists())

    def test_attach_refreshes_color_hint_for_saved_conversation_restart(self):
        self.start(colorterm="")
        wait_for(lambda: self.calls())
        record = json.loads((self.directory() / "session.json").read_text())
        client, terminal = self.attach(colorterm="truecolor")
        os.write(terminal, b"Q")
        client.wait(timeout=3)
        wait_for(lambda: self.call("status")["dead"])
        self.worker.restart(self.module.session_name("trial"))
        wait_for(lambda: len(self.calls()) == 2)
        self.assertEqual(self.calls()[1]["colorterm"], "truecolor")
        self.assertEqual(self.calls()[1]["argv"], ["--session", record["conversation"]])

    def test_default_start_status_list_locate_and_private_long_socket(self):
        name = "a" * 64
        self.start(name, prompt="one prompt")
        call = wait_for(lambda: self.calls())[0]
        record = json.loads((self.directory(name) / "session.json").read_text())
        self.assertEqual(record["backend"], "dtach")
        self.assertEqual(
            call["argv"],
            [
                "--session-id",
                record["conversation"],
                "@" + str(self.directory(name) / "prompt.txt"),
            ],
        )
        self.assertEqual(call["cwd"], str(self.home / "project"))
        self.assertEqual((call["jobs"], call["scripts"]), ("3", "/fixture/build"))
        self.assertEqual(call["pi_dir"], str(self.home / ".config/pi/agent"))
        self.assertEqual(call["runtime"], f"/run/user/{os.getuid()}")
        self.assertEqual(call["bus"], f"unix:path=/run/user/{os.getuid()}/bus")
        self.assertFalse(self.call("status", name)["dead"])
        self.assertTrue(self.call("locate", name)["exists"])
        listing = self.call("list")["sessions"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(
            (listing[0]["name"], listing[0]["project"], listing[0]["harness"]),
            (name, "fixture", "pi"),
        )
        self.assertFalse(listing[0]["attached"])
        for file in self.directory(name).iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_detach_reattach_no_ctrl_l_then_exit_and_resume_without_prompt(self):
        self.start(prompt="once")
        wait_for(lambda: self.calls())
        record = json.loads((self.directory() / "session.json").read_text())
        first, terminal = self.attach()
        os.write(terminal, b"hello")
        wait_for(
            lambda: (
                b"hello" in (self.home / "input").read_bytes()
                if (self.home / "input").exists()
                else False
            )
        )
        os.write(terminal, b"\x1c")
        self.assertEqual(first.wait(timeout=3), 0)
        self.assertFalse(self.call("status")["dead"])
        second, terminal2 = self.attach()
        os.write(terminal2, b"again")
        wait_for(lambda: b"again" in (self.home / "input").read_bytes())
        self.assertNotIn(b"\x0c", (self.home / "input").read_bytes())
        os.write(terminal2, b"Q")
        second.wait(timeout=3)
        wait_for(lambda: self.call("status")["dead"])
        self.assertEqual(self.call("status")["exit_status"], 7)
        self.assertTrue(self.call("list")["sessions"][0]["dead"])
        self.worker.restart(self.module.session_name("trial"))
        wait_for(lambda: len(self.calls()) == 2)
        self.assertEqual(self.calls()[1]["argv"], ["--session", record["conversation"]])
        self.call("cancel")
        self.assertFalse(self.call("locate")["exists"])

    def test_bracketed_multiline_paste_on_first_and_repeat_attach(self):
        (self.home / ".local/bin/pi").write_text(PASTE_FIXTURE)
        self.start()
        state_path = self.home / "paste-state.json"
        initial = wait_for(
            lambda: json.loads(state_path.read_text()) if state_path.exists() else None
        )
        pid = initial["pid"]
        expected_pastes = []
        received = 0
        for attachment in (1, 2):
            client, terminal = self.attach()
            output = self.read(terminal)
            self.assertNotIn(b"STARTUP-ONLY", output)
            enabled = b"\x1b[?2004h" in output
            content = f"paste {attachment}\rsecond line\rthird line"
            # Model a terminal paste using only modes observed on this PTY.
            payload = content.encode()
            if enabled:
                payload = b"\x1b[200~" + payload + b"\x1b[201~"
            os.write(terminal, payload)
            received += len(payload)
            state = wait_for(
                lambda expected_received=received: (
                    snapshot
                    if (snapshot := json.loads(state_path.read_text()))["received"]
                    == expected_received
                    else None
                )
            )
            expected_pastes.append(content)
            with self.subTest(attachment=attachment, behavior="terminal mode"):
                self.assertTrue(enabled, f"attachment output: {output!r}")
            with self.subTest(attachment=attachment, behavior="single paste"):
                self.assertEqual(state["pastes"], expected_pastes)
            with self.subTest(attachment=attachment, behavior="no submissions"):
                self.assertEqual(state["submissions"], [])
            self.assertEqual(state["pid"], pid)
            self.assertTrue(alive(pid))
            os.write(terminal, b"\x1c")
            self.assertEqual(client.wait(timeout=3), 0)
            self.assertFalse(self.call("list")["sessions"][0]["attached"])
            self.assertFalse(self.call("status")["dead"])
            self.assertTrue(alive(pid))

    def test_non_tty_attach_does_not_emit_bracketed_paste_mode(self):
        self.start()
        call = wait_for(lambda: self.calls())[0]
        client, terminal = self.attach(tty_output=False)
        os.write(terminal, b"pipe-input")
        wait_for(
            lambda: (
                b"pipe-input" in (self.home / "input").read_bytes()
                if (self.home / "input").exists()
                else False
            )
        )
        os.write(terminal, b"\x1c")
        self.assertEqual(client.wait(timeout=3), 0)
        output = client.stdout.read()
        self.assertIn(b"ECHO:pipe-input", output)
        self.assertNotIn(b"\x1b[?2004h", output)
        self.assertTrue(alive(call["pid"]))

    def test_other_worker_cancellation_reaps_detached_launcher(self):
        self.start()
        wait_for(lambda: self.calls())
        runtime = json.loads((self.directory() / "runtime.json").read_text())
        owner = self.worker
        self.worker = self.module.Worker(self.home, self.tmux, scope=[])
        self.call("cancel")
        wait_for(lambda: not alive(runtime["master"]["pid"]))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            del owner
            gc.collect()
        self.assertEqual(
            [
                str(item.message)
                for item in caught
                if issubclass(item.category, ResourceWarning)
            ],
            [],
        )

    def test_restart_drains_stopped_old_master_before_new_attachment(self):
        self.start()
        call = wait_for(lambda: self.calls())[0]
        runtime = json.loads((self.directory() / "runtime.json").read_text())
        old_master = runtime["master"]["pid"]
        os.kill(old_master, signal.SIGSTOP)
        os.kill(call["pid"], signal.SIGKILL)
        os.kill(call["child"], signal.SIGKILL)
        wait_for(lambda: self.call("status")["dead"])
        other = self.module.Worker(self.home, self.tmux, scope=[])
        other.restart(self.module.session_name("trial"))
        self.assertFalse(alive(old_master))
        wait_for(lambda: len(self.calls()) == 2)
        attached, terminal = self.attach()
        os.write(terminal, b"new-generation")
        wait_for(
            lambda: (
                b"new-generation" in (self.home / "input").read_bytes()
                if (self.home / "input").exists()
                else False
            )
        )
        os.write(terminal, b"\x1c")
        self.assertEqual(attached.wait(timeout=3), 0)
        other.dispatch({"action": "cancel", "name": "trial"})

    def test_logs_explicitly_unavailable(self):
        self.start()
        with self.assertRaisesRegex(
            RuntimeError, "unavailable.*dtach|dtach.*unavailable"
        ):
            self.call("logs")

    def test_cancel_kills_owned_children_not_unrelated_process(self):
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"]
        )
        try:
            self.start()
            call = wait_for(lambda: self.calls())[0]
            self.call("cancel")
            wait_for(lambda: not alive(call["pid"]) and not alive(call["child"]))
            self.assertIsNone(unrelated.poll())
            self.assertFalse(self.directory().exists())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_reused_identity_never_signalled(self):
        self.start()
        wait_for(lambda: self.calls())
        path = self.directory() / "runtime.json"
        original = path.read_bytes()
        runtime = json.loads(original)
        runtime["process"]["start"] = "not-the-same-process"
        path.write_text(json.dumps(runtime))
        try:
            self.assertTrue(self.call("status")["dead"])
            with self.assertRaisesRegex(RuntimeError, "identity"):
                self.call("cancel")
            self.assertTrue(alive(self.calls()[0]["pid"]))
        finally:
            path.write_bytes(original)

    def test_socket_and_runtime_modes_rejected(self):
        self.start()
        for name in ("runtime.json", "dtach.sock"):
            path = self.directory() / name
            path.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "filesystem"):
                self.call("status")
            path.chmod(0o600)

    def test_missing_binary_fails_without_state_or_tmux_fallback(self):
        original = self.module.shutil.which
        with (
            mock.patch.object(
                self.module.shutil,
                "which",
                side_effect=lambda name, **kwargs: (
                    None if name == "dtach" else original(name, **kwargs)
                ),
            ),
            self.assertRaisesRegex(FileNotFoundError, "dtach"),
        ):
            self.start()
        self.assertFalse(self.directory().exists())

    def test_dead_agent_reports_failure_without_live_wrapper_success(self):
        self.start()
        call = wait_for(lambda: self.calls())[0]
        os.kill(call["pid"], signal.SIGKILL)
        try:
            wait_for(lambda: self.call("status")["dead"])
            status = self.call("status")
            self.assertEqual(status["exit_status"], 137)
        finally:
            if alive(call["child"]):
                os.kill(call["child"], signal.SIGKILL)

    def test_missing_socket_is_dead_even_when_metadata_process_alive(self):
        self.start()
        wait_for(lambda: self.calls())
        (self.directory() / "dtach.sock").unlink()
        self.assertTrue(self.call("status")["dead"])
        self.assertFalse(self.call("list")["sessions"][0]["attached"])

    def test_stalled_startup_is_bounded_and_leaves_no_child(self):
        fake = self.home / ".local/bin/dtach"
        fake.write_text(
            "#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\nPath(os.environ['HOME'], 'launcher-pid').write_text(str(os.getpid()))\ntime.sleep(600)\n"
        )
        fake.chmod(0o700)
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.start()
        self.assertLess(time.monotonic() - start, 7)
        self.assertFalse(alive(int((self.home / "launcher-pid").read_text())))
        self.assertFalse(self.directory().exists())

    def test_runtime_and_socket_symlinks_rejected(self):
        self.start()
        for name in ("runtime.json", "dtach.sock"):
            path = self.directory() / name
            saved = path.with_name("saved")
            path.rename(saved)
            path.symlink_to(saved)
            try:
                with self.assertRaisesRegex(ValueError, "filesystem"):
                    self.call("status")
            finally:
                path.unlink()
                saved.rename(path)

    def test_startup_failure_bounded_and_explicit(self):
        fake = self.home / ".local/bin/dtach"
        fake.write_text(
            "#!/usr/bin/env python3\nimport sys; print('fixture startup failed', file=sys.stderr); sys.exit(23)\n"
        )
        fake.chmod(0o700)
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "fixture startup failed"):
            self.start()
        self.assertLess(time.monotonic() - start, 6)
        self.assertFalse(self.directory().exists())


if __name__ == "__main__":
    unittest.main()

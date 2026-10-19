"""Real user-systemd scope lifetime across ordinary dtach attachment/reopen."""

import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DTACH = shutil.which("dtach")
SCOPE = [
    "systemd-run",
    "--user",
    "--scope",
    "--slice=app.slice",
    "--quiet",
    "--collect",
    "--property=MemoryMax=134217728",
    "--property=TasksMax=32",
    "--property=CPUQuota=50%",
]
FIXTURE = """#!/usr/bin/env python3
import json, os, subprocess, sys, tty
from pathlib import Path
tty.setraw(0)
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(600)"],
    start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
home = Path(os.environ["HOME"])
with (home / "calls").open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "child": child.pid,
        "cgroup": Path("/proc/self/cgroup").read_text()}) + "\\n")
while True:
    if b"Q" in os.read(0, 1024):
        sys.exit(0)
"""


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("bounded lifecycle wait expired")


@unittest.skipUnless(
    DTACH and shutil.which("systemd-run") and shutil.which("tmux"),
    "real dtach, tmux and user systemd required",
)
class DtachScopeTests(unittest.TestCase):
    def setUp(self):
        available = subprocess.run(
            ["systemctl", "--user", "show", "--property=Version", "--value"],
            capture_output=True,
            text=True,
        )
        if available.returncode:
            self.skipTest("user systemd unavailable: " + available.stderr)
        self.temp = tempfile.TemporaryDirectory(prefix="ra-scope-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True)
        (config / "remote-hosts.json").write_text(
            json.dumps({"desktop": {"cargo_build_jobs": "3"}})
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
        environment = mock.patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)
        spec = importlib.util.spec_from_file_location(
            "scope_worker", ROOT / "remote-agent-worker.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.name = self.home.name
        self.session = self.module.session_name(self.name)
        self.worker = self.module.Worker(
            self.home,
            ["tmux", "-L", self.name, "-f", "/dev/null"],
            scope=SCOPE,
        )
        self.unit = self.session + "-dtach.scope"
        self.memory_limit = "134217728"
        self.children = []
        self.addCleanup(self.clean_fixture)

    def clean_fixture(self):
        # Only this fixture's recorded children and managed dtach tree are owned.
        try:
            if self.worker.read_record(self.session):
                self.worker.dispatch({"action": "cancel", "name": self.name})
        finally:
            for identity in self.children:
                if self.module.process_identity(identity["pid"]) == identity:
                    os.kill(identity["pid"], signal.SIGKILL)
            # This random fixture unit owns every override; never revert a real unit.
            subprocess.run(
                ["systemctl", "--user", "stop", self.unit], capture_output=True
            )
            subprocess.run(
                ["systemctl", "--user", "revert", self.unit],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["systemctl", "--user", "reset-failed", self.unit], capture_output=True
            )

    def calls(self):
        path = self.home / "calls"
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text().splitlines()]
        for call in calls:
            identity = self.module.process_identity(call["child"])
            if identity and identity not in self.children:
                self.children.append(identity)
        return calls

    def attach(self):
        # The public attach action performs its normal dead-session restart and
        # execs real dtach. Only the isolated fixture's scope configuration differs.
        script = (
            "import importlib.util; "
            f"s=importlib.util.spec_from_file_location('worker', {str(ROOT / 'remote-agent-worker.py')!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            f"m.Worker(scope={SCOPE!r}).dispatch({{'action':'attach','name':{self.name!r}}})"
        )
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                env=self.env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(process.wait, timeout=5)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process, master

    @staticmethod
    def read_terminal(fd):
        output = b""
        while select.select([fd], [], [], 0.1)[0]:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            output += data
        return output.decode(errors="replace")

    def properties(self):
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                self.unit,
                "-p",
                "ActiveState",
                "-p",
                "LoadState",
                "-p",
                "Delegate",
                "-p",
                "InvocationID",
                "-p",
                "ControlGroup",
                "-p",
                "DropInPaths",
                "-p",
                "Slice",
                "-p",
                "MemoryMax",
                "-p",
                "TasksMax",
                "-p",
                "CPUQuotaPerSecUSec",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def assert_limits(self, properties):
        self.assertEqual(properties["Slice"], "app.slice")
        self.assertEqual(properties["MemoryMax"], self.memory_limit)
        self.assertEqual(properties["TasksMax"], "32")
        self.assertEqual(properties["CPUQuotaPerSecUSec"], "500ms")

    def quit_agent(self):
        attached, terminal = self.attach()
        wait_for(lambda: self.worker.dtach_status(self.session)["attached"])
        os.write(terminal, b"Q")
        self.assertEqual(attached.wait(timeout=8), 0, self.read_terminal(terminal))
        wait_for(lambda: self.worker.dtach_status(self.session)["dead"])
        self.assertEqual(self.worker.dtach_status(self.session)["exit_status"], 0)

    def reopen(self, count):
        attached, terminal = self.attach()
        wait_for(lambda: len(self.calls()) == count or attached.poll() is not None)
        self.assertEqual(len(self.calls()), count, self.read_terminal(terminal))
        wait_for(lambda: self.worker.dtach_status(self.session)["attached"])
        os.write(terminal, b"Q")
        self.assertEqual(attached.wait(timeout=8), 0)
        wait_for(lambda: self.worker.dtach_status(self.session)["dead"])

    def test_01_legacy_nondelegated_scope_attaches_real_new_children(self):
        self.memory_limit = "100663296"
        marker = self.home / "legacy-pid"
        launcher = subprocess.Popen(
            [
                *SCOPE,
                "--property=MemoryMax=" + self.memory_limit,
                "--unit=" + self.unit,
                "--",
                sys.executable,
                "-c",
                "import os; from pathlib import Path; "
                f"Path({str(marker)!r}).write_text(str(os.getpid())); "
                "os.execlp('sleep', 'sleep', '600')",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(launcher.wait, timeout=5)
        self.addCleanup(lambda: launcher.poll() is None and launcher.kill())
        wait_for(marker.exists)
        legacy = self.module.process_identity(int(marker.read_text()))
        self.children.append(legacy)
        before = self.properties()
        parent_cgroup = Path("/proc/self/cgroup").read_text()
        self.assertEqual(before["Delegate"], "no")
        self.assert_limits(before)
        self.exercise_reopens()
        after = self.properties()
        self.assertEqual(after["Delegate"], "yes")
        self.assertEqual(after["InvocationID"], before["InvocationID"])
        self.assertEqual(after["ControlGroup"], before["ControlGroup"])
        self.assertEqual(self.module.process_identity(legacy["pid"]), legacy)
        self.assertEqual(Path("/proc/self/cgroup").read_text(), parent_cgroup)
        self.assertEqual(len(after["DropInPaths"].split()), 1)
        self.assert_limits(after)
        self.recreate_collected_scope()
        self.assertEqual(self.properties()["DropInPaths"], "")

    def test_02_new_scope_collects_and_recreates_same_name(self):
        self.exercise_reopens()
        self.recreate_collected_scope()

    def recreate_collected_scope(self):
        before = self.properties()
        self.assertEqual(before["Delegate"], "yes")
        for identity in self.children:
            os.kill(identity["pid"], signal.SIGKILL)
        wait_for(lambda: self.properties()["ActiveState"] == "inactive")
        wait_for(lambda: not Path("/sys/fs/cgroup" + before["ControlGroup"]).exists())
        self.reopen(4)
        after = self.properties()
        self.assertNotEqual(after["InvocationID"], before["InvocationID"])
        self.assertEqual(after["ControlGroup"], before["ControlGroup"])
        self.assertEqual(after["Delegate"], "yes")
        # Recreation takes configured limits; reuse above retained the old limits.
        self.memory_limit = "134217728"
        self.assert_limits(after)
        conversation = self.worker.read_record(self.session)["conversation"]
        self.assertEqual(self.calls()[-1]["argv"], ["--session", conversation])

    def test_03_scope_api_failure_is_reported_before_start_succeeds(self):
        self.worker.scope = [*SCOPE, "--property=NoSuchRemoteAgentProperty=yes"]
        with self.assertRaisesRegex(RuntimeError, "scope.*failed|scope.*exited"):
            self.worker.dispatch(
                {"action": "start", "name": self.name, "repo": "fixture", "agent": "pi"}
            )
        self.assertEqual(self.calls(), [])

    def test_04_unsupported_attach_api_reports_error_without_new_scope(self):
        self.exercise_reopens()
        before = self.properties()
        binary = self.home / ".local/bin/busctl"
        binary.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "print('AttachProcessesToUnit unsupported fixture', file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
        binary.chmod(0o700)
        with mock.patch.dict(
            os.environ, {"PATH": str(binary.parent) + ":" + os.environ["PATH"]}
        ):
            with self.assertRaisesRegex(
                RuntimeError, "AttachProcessesToUnit unsupported fixture"
            ):
                self.worker.restart(self.session)
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.properties()["InvocationID"], before["InvocationID"])
        self.assertTrue(
            all(self.module.identity_alive(identity) for identity in self.children)
        )

    def exercise_reopens(self):
        self.worker.dispatch(
            {"action": "start", "name": self.name, "repo": "fixture", "agent": "pi"}
        )
        first = wait_for(self.calls)[0]
        conversation = self.worker.read_record(self.session)["conversation"]
        self.quit_agent()
        child = self.module.process_identity(first["child"])
        self.assertEqual(self.module.process_identity(first["child"]), child)
        self.assertIn(
            first["cgroup"].strip().split(":", 2)[2],
            Path(f"/proc/{first['child']}/cgroup").read_text(),
        )
        old_unit = first["cgroup"].strip().rsplit("/", 1)[1]
        state = subprocess.run(
            ["systemctl", "--user", "is-active", old_unit],
            capture_output=True,
            text=True,
        )
        self.assertEqual(state.stdout.strip(), "active", state.stderr)

        initial = self.properties()
        reopened, second_terminal = self.attach()
        wait_for(lambda: len(self.calls()) == 2 or reopened.poll() is not None)
        calls = self.calls()
        self.assertEqual(
            len(calls),
            2,
            "ordinary attach failed to reopen while old scope remained active: "
            + self.read_terminal(second_terminal)
            + repr(self.worker.dtach_status(self.session)),
        )
        self.assertEqual(calls[0]["argv"], ["--session-id", conversation])
        self.assertEqual(calls[1]["argv"], ["--session", conversation])
        self.assertEqual(calls[0]["cgroup"], calls[1]["cgroup"])
        self.assertEqual(self.module.process_identity(first["child"]), child)
        self.assertEqual(old_unit, self.unit)
        self.assert_limits(self.properties())
        wait_for(lambda: self.worker.dtach_status(self.session)["attached"])
        os.write(second_terminal, b"Q")
        self.assertEqual(reopened.wait(timeout=8), 0)
        wait_for(lambda: self.worker.dtach_status(self.session)["dead"])
        self.assertEqual(self.module.process_identity(first["child"]), child)
        self.reopen(3)
        current = self.properties()
        self.assertEqual(current["InvocationID"], initial["InvocationID"])
        self.assertEqual(current["DropInPaths"], initial["DropInPaths"])
        self.assertTrue(all(call["cgroup"] == first["cgroup"] for call in self.calls()))
        self.assertTrue(
            all(self.module.identity_alive(identity) for identity in self.children)
        )
        units = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--all",
                "--plain",
                "--no-legend",
                self.session + "*",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertEqual([line.split()[0] for line in units], [self.unit])


if __name__ == "__main__":
    unittest.main()

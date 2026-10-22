"""Behavioral tests: real detached workers, fixture agents, no model calls."""

import base64
import fcntl
import importlib.util
import io
import json
import os
import pty
import select
import signal
import stat
import struct
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


FIXTURE_HOSTS = {
    "desktop": {
        "ssh": "desktop",
        "home": "/home/fixture-test",
        "wrap": ["wsl.exe", "-d", "FixtureBuild", "-u", "fixture-test", "--exec"],
        "windows_shell": True,
        "cargo_build_jobs": "6",
    },
    "agent-server": {
        "ssh": "agent-server",
        "home": "/home/fixture",
        "wrap": [],
        "windows_shell": False,
        "cargo_build_jobs": "20",
    },
}
FIXTURE_PROJECTS = {
    "engine": {
        "remote": "Projects/sample-game/game-engine",
        "local": "/fixture/engine",
    },
    "ui-demo": {"remote": "Projects/demo/ui-demo", "local": "/fixture/ui"},
    "server": {
        "remote": "Projects/sample-game/game-server",
        "local": "/fixture/server",
    },
}


def write_config(home, projects=None):
    config = home / ".config/remote-agent"
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in (
        ("remote-hosts.json", FIXTURE_HOSTS),
        ("remote-projects.json", FIXTURE_PROJECTS if projects is None else projects),
    ):
        path = config / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)
    return config


def isolate_home(test, home):
    write_config(home)
    patch = mock.patch.dict(os.environ, {"HOME": str(home)})
    patch.start()
    test.addCleanup(patch.stop)


def load_worker():
    path = ROOT / "remote-agent-worker.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("remote_agent_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = write_config(
            self.home, {"fixture": {"remote": "Projects/fixture", "local": "/fixture"}}
        )
        self.code = self.home / "code"
        self.code.mkdir()
        for name in ("remote-agent.py", "remote-agent-worker.py", "deploy.py"):
            shutil.copy(ROOT / name, self.code)
        for name in ("remote-hosts.json", "remote-projects.json"):
            (self.code / name).write_text("{}")
        self.env = {**os.environ, "HOME": str(self.home)}

    def invoke(self, name, *args, request=None):
        return subprocess.run(
            [sys.executable, str(self.code / name), *args],
            input=json.dumps(request) if request is not None else None,
            env=self.env,
            text=True,
            capture_output=True,
        )

    def test_controller_uses_user_project_mapping(self):
        result = self.invoke("remote-agent.py", "projects")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)["projects"]), {"fixture"})

    def test_worker_uses_user_project_mapping(self):
        result = self.invoke(
            "remote-agent-worker.py",
            "--host",
            "agent-server",
            request={"action": "projects"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "projects": {
                    "fixture": {"remote": "Projects/fixture", "local": "/fixture"}
                }
            },
        )

    def test_missing_user_hosts_fails_without_repo_fallback(self):
        (self.config / "remote-hosts.json").unlink()
        for name, args in (
            ("remote-agent.py", ("projects",)),
            ("remote-agent-worker.py", ()),
            ("deploy.py", ("--help",)),
        ):
            with self.subTest(name=name):
                result = self.invoke(name, *args, request={"action": "projects"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(self.config / "remote-hosts.json"), result.stderr)

    def test_missing_user_projects_fails_without_repo_fallback(self):
        (self.config / "remote-projects.json").unlink()
        for name, args in (
            ("remote-agent.py", ("projects",)),
            ("remote-agent-worker.py", ()),
        ):
            with self.subTest(name=name):
                result = self.invoke(name, *args, request={"action": "projects"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    str(self.config / "remote-projects.json"),
                    result.stdout + result.stderr,
                )


class PeriodicSchedulerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node runtime required")
    def test_periodic_scheduler_runtime(self):
        result = subprocess.run(
            ["node", "--test", str(ROOT / "tests/test_periodic_scheduler.mjs")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        isolate_home(self, self.home)
        self.module = load_worker()
        account = self.home / ".config/pi-claude-bridge/profiles/fixture"
        account.mkdir(parents=True, mode=0o700)
        self.module.write_private(account / ".credential-owner", "shared-v1\n")
        self.module.write_private(
            account / ".credentials.json",
            json.dumps({"claudeAiOauth": {"accessToken": "fixture-native-account"}}),
        )
        (self.home / ".claude").mkdir(mode=0o700)
        self.module.write_private(self.home / ".claude/.active-profile", "fixture")
        os.environ.pop("BUILD_HOST_SCRIPTS", None)
        self.assertIsNotNone(self.module, "remote worker is not implemented")
        self.tmux = [
            shutil.which("tmux"),
            "-L",
            "agent-test-" + self.home.name,
            "-f",
            "/dev/null",
        ]
        self.addCleanup(
            lambda: subprocess.run(self.tmux + ["kill-server"], capture_output=True)
        )
        self.worker = self.module.Worker(self.home, self.tmux, scope=[])
        self.addCleanup(self.cancel_sessions)
        self.observation = self.home / "observation.json"
        self.engine = self.home / "Projects/sample-game/game-engine"
        self.ui = self.home / "Projects/demo/ui-demo"
        self.engine.mkdir(parents=True)
        self.ui.mkdir(parents=True)
        bindir = self.home / ".local/bin"
        bindir.mkdir(parents=True)
        fixture = f"""#!{sys.executable}
import json, os, pathlib, sys, time
args = sys.argv[1:]
conversation = []
if args[:1] in (['--session-id'], ['--resume'], ['--session']):
    conversation, args = args[:2], args[2:]
extensions = []
while args[:1] == ['--extension']:
    extensions.append(args[1])
    args = args[2:]
if args[:1] == ['--']:
    args = args[1:]
prompt = None
if args:
    prompt = pathlib.Path(args[0][1:]).read_text() if args[0].startswith('@') else args[0]
observation = json.dumps({{"argv": args, "extensions": extensions, "executable": pathlib.Path(sys.argv[0]).name, "conversation": conversation, "prompt": prompt, "cwd": os.getcwd(), "path": os.environ["PATH"], "scripts": os.environ.get("BUILD_HOST_SCRIPTS"), "pi_dir": os.environ.get("PI_CODING_AGENT_DIR"), "runtime_dir": os.environ.get("XDG_RUNTIME_DIR"), "cargo_jobs": os.environ.get("CARGO_BUILD_JOBS"), "cgroup": pathlib.Path("/proc/self/cgroup").read_text()}})
pathlib.Path({str(self.observation)!r}).write_text(observation)
print(observation, flush=True)
if prompt == "hold":
    while True:
        time.sleep(0.05)
sys.exit(7)
"""
        for name in ("pi", "i"):
            executable = bindir / name
            executable.write_text(fixture)
            executable.chmod(0o700)

    def cancel_sessions(self):
        if self.worker.state.is_dir():
            for directory in self.worker.state.iterdir():
                if directory.is_dir() and directory.name.startswith("remote-agent-"):
                    self.worker.dispatch(
                        {
                            "action": "cancel",
                            "name": directory.name.removeprefix("remote-agent-"),
                        }
                    )

    def restart(self, worker=None):
        self.observation.unlink(missing_ok=True)
        (worker or self.worker).restart("remote-agent-job")

    def start(self, name="job", repo="engine", agent="pi", prompt=None):
        self.observation.unlink(missing_ok=True)
        request = {"action": "start", "name": name, "repo": repo, "agent": agent}
        if prompt is not None:
            request["prompt"] = prompt
        return self.worker.dispatch(request)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.03)
        self.fail("worker fixture did not reach expected state")

    def status(self, name="job"):
        return self.worker.dispatch({"action": "status", "name": name})

    def logs(self, name="job"):
        return self.worker.dispatch({"action": "logs", "name": name})["logs"]

    def output(self):
        def read():
            try:
                return json.loads(self.observation.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                return None

        return self.wait_for(read)


class ProjectPiExecutableTests(WorkerFixture):
    def start(self, name="job", repo="engine", agent="pi", prompt=None, resume=None):
        # Exercise real legacy tmux launches without requiring a host detach install.
        self.worker.agent_launch(repo, agent)
        self.observation.unlink(missing_ok=True)
        session = "remote-agent-" + name
        directory = self.worker.create_directory(session)
        conversation = resume or "fixture-conversation"
        self.module.write_private(
            directory / "session.json",
            json.dumps({"repo": repo, "agent": agent, "conversation": conversation}),
        )
        prompt_file = None
        if prompt is not None:
            prompt_file = directory / "prompt.txt"
            self.module.write_private(prompt_file, prompt)
        self.worker.launch(
            session,
            repo,
            agent,
            [
                self.module.RESUME_FLAGS[agent] if resume else "--session-id",
                conversation,
            ],
            prompt_file,
        )

    def test_pi_cancel_hook_runs_after_tmux_stop_with_recorded_conversation(self):
        from pi_cancel_fixture import configure_hook, hook_calls

        self.start(prompt="hold", resume="selected-conversation")
        self.output()
        pid = int(
            self.worker.run_tmux(
                "display-message", "-p", "-t", "=remote-agent-job:", "#{pane_pid}"
            ).stdout
        )
        directory = self.worker.state / "remote-agent-job"
        configure_hook(self.worker, "engine", self.home, directory, [pid])
        self.worker.dispatch({"action": "cancel", "name": "job"})
        calls = hook_calls(self.home)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["selected-conversation"])
        self.assertEqual(calls[0]["record"]["conversation"], "selected-conversation")
        self.assertEqual(calls[0]["alive"], [False])
        self.assertFalse(directory.exists())

    def test_pi_cancel_hook_failure_preserves_state_and_retries_without_tmux(self):
        from pi_cancel_fixture import configure_hook, hook_calls

        self.start(prompt="hold")
        self.output()
        directory = self.worker.state / "remote-agent-job"
        configure_hook(self.worker, "engine", self.home, directory)
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        failure = self.home / "cancel-fail"
        failure.touch()
        self.addCleanup(failure.unlink, missing_ok=True)
        with self.assertRaisesRegex(
            RuntimeError, "pi_cancel_executable.*9.*fixture cleanup refused"
        ):
            self.worker.dispatch({"action": "cancel", "name": "job"})
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)
        self.assertNotEqual(
            self.worker.run_tmux(
                "has-session", "-t", "=remote-agent-job", check=False
            ).returncode,
            0,
        )
        failure.unlink()
        self.worker.dispatch({"action": "cancel", "name": "job"})
        self.assertEqual(
            [c["argv"] for c in hook_calls(self.home)], [["fixture-conversation"]] * 2
        )
        self.assertFalse(directory.exists())

    def test_pi_cancel_validates_hook_and_recorded_conversation(self):
        self.start()
        self.output()
        directory = self.worker.state / "remote-agent-job"
        record_file = directory / "session.json"
        original = record_file.read_bytes()
        nonexecutable = self.home / "not-executable"
        nonexecutable.touch()
        self.addCleanup(
            self.worker.projects["engine"].pop, "pi_cancel_executable", None
        )
        self.addCleanup(
            lambda: record_file.exists() and record_file.write_bytes(original)
        )
        for value in (
            None,
            "",
            "   ",
            7,
            False,
            [],
            {},
            "relative/pi",
            str(self.home / "missing"),
            str(self.home),
            str(nonexecutable),
            "bad\0path",
        ):
            self.worker.projects["engine"]["pi_cancel_executable"] = value
            with self.assertRaisesRegex(
                ValueError, "pi_cancel_executable", msg=repr(value)
            ):
                self.worker.dispatch({"action": "cancel", "name": "job"})
            self.assertEqual(record_file.read_bytes(), original)
        self.worker.projects["engine"]["pi_cancel_executable"] = str(
            self.home / ".local/bin/pi"
        )
        for value in (
            None,
            "",
            7,
            "-option",
            "../other",
            "bad;touch marker",
            "x" * 129,
        ):
            with self.subTest(conversation=value):
                record = json.loads(original)
                record["conversation"] = value
                record_file.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "conversation"):
                    self.worker.dispatch({"action": "cancel", "name": "job"})
                self.assertTrue(record_file.exists())
        record = json.loads(original)
        del record["conversation"]
        record_file.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "conversation"):
            self.worker.dispatch({"action": "cancel", "name": "job"})

    def test_claude_cancel_ignores_pi_cancel_hook(self):
        from pi_cancel_fixture import configure_hook

        self.start(agent="claude", prompt="hold")
        self.output()
        directory = self.worker.state / "remote-agent-job"
        configure_hook(self.worker, "engine", self.home, directory)
        self.worker.dispatch({"action": "cancel", "name": "job"})
        self.assertFalse((self.home / "cancel-calls").exists())
        self.assertFalse(directory.exists())

    def test_project_pi_executable_routes_new_resume_and_restart(self):
        wrapper = self.home / "host wrappers/project-pi"
        wrapper.parent.mkdir()
        shutil.copy(self.home / ".local/bin/pi", wrapper)
        self.worker.projects["engine"]["pi_executable"] = str(wrapper)
        self.start(prompt="literal task")
        self.wait_for(lambda: self.status()["dead"])
        first = self.output()
        self.assertEqual(first["executable"], "project-pi")
        self.assertEqual(first["conversation"][0], "--session-id")
        self.assertEqual(first["prompt"], "literal task")
        self.assertEqual(first["cwd"], str(self.engine))
        self.assertTrue(
            first["path"].startswith(f"{self.home}/.local/bin:{self.home}/.cargo/bin:")
        )
        self.assertEqual(first["pi_dir"], str(self.home / ".config/pi/agent"))

        replacement = wrapper.with_name("replacement-pi")
        shutil.copy(wrapper, replacement)
        self.worker.projects["engine"]["pi_executable"] = str(replacement)
        self.worker.run_tmux("kill-session", "-t", "=remote-agent-job")
        self.restart()
        self.wait_for(lambda: self.status()["dead"])
        resumed = self.output()
        self.assertEqual(resumed["executable"], "replacement-pi")
        self.assertEqual(
            resumed["conversation"], ["--session", first["conversation"][1]]
        )
        self.assertIsNone(resumed["prompt"])
        for key in ("cwd", "path", "pi_dir", "cargo_jobs", "runtime_dir"):
            self.assertEqual(resumed[key], first[key])
        self.worker.dispatch({"action": "cancel", "name": "job"})
        self.observation.unlink()
        self.start(resume="saved-conversation")
        self.wait_for(lambda: self.status()["dead"])
        self.assertEqual(self.output()["executable"], "replacement-pi")
        self.assertEqual(
            self.output()["conversation"], ["--session", "saved-conversation"]
        )

    def test_invalid_project_pi_executable_fails_without_starting_default(self):
        nonexecutable = self.home / "not-executable"
        nonexecutable.write_text("not executable")
        for value in (
            None,
            "",
            "   ",
            7,
            False,
            [],
            {},
            "relative/pi",
            str(self.home / "missing"),
            str(self.home),
            str(nonexecutable),
            str(self.home / "bad\0path"),
        ):
            with self.subTest(value=value):
                self.worker.projects["engine"]["pi_executable"] = value
                with self.assertRaisesRegex(ValueError, "pi_executable"):
                    self.worker.agent_launch("engine", "pi")
                self.assertFalse(self.observation.exists())
                self.assertFalse((self.worker.state / "remote-agent-job").exists())

    def test_project_pi_executable_absent_keeps_default_and_claude_ignores_it(self):
        for agent, configured, executable in (
            ("pi", False, "pi"),
            ("claude", True, "i"),
        ):
            with self.subTest(agent=agent):
                if configured:
                    self.worker.projects["engine"]["pi_executable"] = None
                self.start(agent=agent)
                self.wait_for(lambda: self.status()["dead"])
                self.assertEqual(self.output()["executable"], executable)
                self.worker.dispatch({"action": "cancel", "name": "job"})


class WorkerTests(WorkerFixture):
    def test_build_scripts_absent_without_caller_value(self):
        self.start()
        self.wait_for(lambda: self.status()["dead"])
        self.assertIsNone(self.output()["scripts"])

    def test_build_scripts_absent_preserves_inherited_caller_value(self):
        os.environ["BUILD_HOST_SCRIPTS"] = "/fixture/caller scripts"
        self.start()
        self.wait_for(lambda: self.status()["dead"])
        self.assertEqual(self.output()["scripts"], "/fixture/caller scripts")

    def test_build_scripts_selected_profile_reaches_launch_and_restart(self):
        hosts = json.loads(json.dumps(FIXTURE_HOSTS))
        hosts["desktop"]["build_host_scripts"] = "/fixture/desktop scripts"
        hosts["agent-server"]["build_host_scripts"] = "/fixture/server scripts"
        (self.home / ".config/remote-agent/remote-hosts.json").write_text(
            json.dumps(hosts)
        )
        os.environ["BUILD_HOST_SCRIPTS"] = "/fixture/caller scripts"
        module = load_worker()
        for host in hosts:
            with self.subTest(host=host):
                self.worker = module.Worker(self.home, self.tmux, scope=[], host=host)
                self.start()
                self.wait_for(lambda: self.status()["dead"])
                self.assertEqual(
                    self.output()["scripts"], hosts[host]["build_host_scripts"]
                )
                self.restart()
                self.wait_for(lambda: self.status()["dead"])
                self.assertEqual(
                    self.output()["scripts"], hosts[host]["build_host_scripts"]
                )
                self.worker.dispatch({"action": "cancel", "name": "job"})

    def test_controller_harness_selection_reaches_agent_argv(self):
        spec = importlib.util.spec_from_file_location(
            "remote_agent_controller", ROOT / "remote-agent.py"
        )
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        for options, harness, executable in (
            ([], "pi", "pi"),
            (["--agent-harness", "claude"], "claude", "i"),
        ):
            with self.subTest(harness=harness):
                with mock.patch.object(
                    sys, "argv", ["remote-agent", "start", "engine", "job", *options]
                ):
                    args = controller.parse_args()
                self.worker.dispatch(
                    {
                        "action": args.action,
                        "repo": args.project,
                        "name": args.name,
                        "agent": args.agent_harness,
                        "prompt": "literal task",
                    }
                )
                self.wait_for(lambda: self.status()["dead"])
                output = self.output()
                self.worker.dispatch({"action": "cancel", "name": "job"})
                self.assertEqual(output["executable"], executable)
                self.assertEqual(output["conversation"][0], "--session-id")
                self.assertEqual(output["prompt"], "literal task")
                if harness == "pi":
                    self.assertEqual(len(output["argv"]), 1)
                    self.assertTrue(output["argv"][0].startswith("@"))
                else:
                    self.assertEqual(output["argv"], ["literal task"])

    def test_user_extension_launch_and_restart_without_explicit_injection(self):
        for repo, agent in (
            ("engine", "pi"),
            ("server", "pi"),
            ("ui-demo", "pi"),
            ("engine", "claude"),
        ):
            with self.subTest(repo=repo, agent=agent):
                (self.home / self.worker.projects[repo]["remote"]).mkdir(
                    parents=True, exist_ok=True
                )
                self.start(repo=repo, agent=agent, prompt="literal task")
                self.wait_for(lambda: self.status()["dead"])
                first = self.output()
                self.restart()
                self.wait_for(lambda: self.status()["dead"])
                resumed = self.output()
                self.worker.dispatch({"action": "cancel", "name": "job"})
                self.assertEqual(first["extensions"], [])
                for output in (first, resumed):
                    self.assertIsNone(output["scripts"])
                    self.assertEqual(
                        output["pi_dir"], str(self.home / ".config/pi/agent")
                    )
                    self.assertEqual(output["runtime_dir"], f"/run/user/{os.getuid()}")
                    self.assertEqual(output["cargo_jobs"], "6")
                    self.assertEqual(
                        output["cwd"],
                        str(self.home / self.worker.projects[repo]["remote"]),
                    )
                    self.assertTrue(
                        output["path"].startswith(
                            f"{self.home}/.local/bin:{self.home}/.cargo/bin:"
                        )
                    )
                self.assertEqual(first["conversation"][0], "--session-id")
                self.assertEqual(first["prompt"], "literal task")
                self.assertEqual(resumed["extensions"], first["extensions"])
                self.assertEqual(
                    resumed["conversation"],
                    [self.module.RESUME_FLAGS[agent], first["conversation"][1]],
                )
                self.assertIsNone(resumed["prompt"])

    def test_user_extension_pi_resume_without_explicit_injection(self):
        self.worker.dispatch(
            {
                "action": "start",
                "name": "job",
                "repo": "engine",
                "agent": "pi",
                "resume": "existing-conversation",
            }
        )
        self.wait_for(lambda: self.status()["dead"])
        output = self.output()
        self.assertEqual(output["conversation"], ["--session", "existing-conversation"])
        self.assertEqual(output["extensions"], [])
        self.assertIsNone(output["prompt"])

    def test_pi_prompt_exit_output_cwd_and_private_file(self):
        prompt = "literal ; $(touch bad)\nquoted ' text é"
        self.start(prompt=prompt)
        self.wait_for(lambda: self.status()["dead"])
        state = self.status()
        self.assertEqual(state["cwd"], str(self.engine))
        self.assertEqual(state["exit_status"], 7)
        output = self.output()
        self.assertEqual(output["prompt"], prompt)
        self.assertEqual(output["cwd"], str(self.engine))
        self.assertIsNone(output["scripts"])
        self.assertEqual(output["pi_dir"], str(self.home / ".config/pi/agent"))
        self.assertEqual(output["runtime_dir"], f"/run/user/{os.getuid()}")
        self.assertTrue(
            output["path"].startswith(f"{self.home}/.local/bin:{self.home}/.cargo/bin:")
        )
        prompt_file = Path(output["argv"][0][1:])
        self.assertEqual(prompt_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prompt_file.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.engine / "bad").exists())

    def test_session_runs_in_the_capped_slice_with_limited_build_jobs(self):
        slice_name = f"remote-agent-test-{os.getpid()}.slice"
        self.addCleanup(
            lambda: subprocess.run(
                ["systemctl", "--user", "stop", slice_name], capture_output=True
            )
        )
        scope = [
            "systemd-run",
            "--user",
            "--scope",
            f"--slice={slice_name}",
            "--quiet",
            "--collect",
        ]
        worker = self.module.Worker(self.home, self.tmux, scope=scope)
        worker.dispatch(
            {"action": "start", "name": "capped", "repo": "engine", "agent": "pi"}
        )
        output = self.output()
        self.assertIn(
            f"/{slice_name}/remote-agent-capped-dtach.scope", output["cgroup"]
        )
        self.assertEqual(output["cargo_jobs"], "6")

    def test_worker_cli_reports_slice_probe_failure_as_json(self):
        bindir = self.home / ".local/bin"
        systemctl = bindir / "systemctl"
        systemctl.write_text(
            f'#!{sys.executable}\nimport sys\nprint("slice manager unavailable", file=sys.stderr)\nsys.exit(1)\n'
        )
        systemctl.chmod(0o700)
        tmux = bindir / "tmux"
        tmux.write_text(
            f"#!{sys.executable}\nimport os,sys\nos.execv({self.tmux[0]!r}, {self.tmux!r}+sys.argv[1:])\n"
        )
        tmux.chmod(0o700)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "remote-agent-worker.py"),
                "--host",
                "agent-server",
            ],
            input=json.dumps(
                {"action": "start", "name": "job", "repo": "ui-demo", "agent": "pi"}
            ),
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "HOME": str(self.home),
                "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("slice manager unavailable", json.loads(result.stdout)["error"])
        self.assertFalse(
            (self.home / ".local/state/remote-agent/remote-agent-job").exists()
        )

    def test_worker_selects_host_provided_scope_manager(self):
        bindir = self.home / ".local/bin"
        systemctl = bindir / "systemctl"
        systemctl.write_text(
            f"#!{sys.executable}\nimport os,sys\n"
            'if any(arg.endswith(".scope") for arg in sys.argv):\n'
            '    print("ActiveState=inactive\\nDelegate=no\\nDropInPaths=")\n'
            "else:\n"
            '    print(os.environ.get("SYSTEM_SLICE", ""))\n'
        )
        systemctl.chmod(0o700)
        scope_runner = bindir / "systemd-run"
        scope_runner.write_text(f"""#!{sys.executable}
import json,os,sys
args=sys.argv[1:]
command=args.index('--')
with open({str(self.home / "scope.json")!r}, 'w') as stream:
    json.dump(args[:command], stream)
os.execvp(args[command+1], args[command+1:])
""")
        scope_runner.chmod(0o700)
        for fragment, user in (("", True), ("/etc/systemd/system/agents.slice", False)):
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
                    "SYSTEM_SLICE": fragment,
                },
            ):
                worker = self.module.Worker(self.home, self.tmux, host="agent-server")
                worker.dispatch(
                    {
                        "action": "start",
                        "name": "job",
                        "repo": "ui-demo",
                        "agent": "pi",
                    }
                )
            self.wait_for(lambda: self.status()["dead"])
            self.assertEqual(self.output()["cargo_jobs"], "20")
            scope = json.loads((self.home / "scope.json").read_text())
            self.assertEqual("--user" in scope, user)
            self.assertIn("--slice=agents.slice", scope)
            self.assertIn("--unit=remote-agent-job-dtach", scope)
            worker.dispatch({"action": "cancel", "name": "job"})

    def test_native_profile_build_jobs_survive_restart(self):
        worker = self.module.Worker(self.home, self.tmux, scope=[], host="agent-server")
        worker.dispatch(
            {"action": "start", "name": "job", "repo": "ui-demo", "agent": "pi"}
        )
        self.wait_for(lambda: self.status()["dead"])
        self.assertEqual(self.output()["cargo_jobs"], "20")
        self.assertEqual(
            worker.dispatch({"action": "locate", "name": "job"}), {"exists": True}
        )
        self.restart(worker)
        self.wait_for(lambda: self.status()["dead"])
        self.assertEqual(self.output()["cargo_jobs"], "20")
        self.assertEqual(
            worker.dispatch({"action": "locate", "name": "missing"}), {"exists": False}
        )

    def test_resume_passes_harness_specific_flag(self):
        for agent, flag in (("claude", "--resume"), ("pi", "--session")):
            self.worker.dispatch(
                {
                    "action": "start",
                    "name": "job",
                    "repo": "engine",
                    "agent": agent,
                    "resume": "f86e1850-973d",
                }
            )
            self.wait_for(lambda: self.status()["dead"])
            output = self.output()
            self.assertEqual(output["conversation"], [flag, "f86e1850-973d"])
            self.assertEqual(output["argv"], [])
            self.worker.dispatch({"action": "cancel", "name": "job"})
        for bad in ("-x", "a b", "", 3, "x;y"):
            with self.assertRaisesRegex(ValueError, "resume"):
                self.worker.dispatch(
                    {
                        "action": "start",
                        "name": "job",
                        "repo": "engine",
                        "agent": "pi",
                        "resume": bad,
                    }
                )

    def test_claude_literal_prompt_in_ui(self):
        prompt = "-literal ; 'quoted'\nsecond line"
        self.start(repo="ui-demo", agent="claude", prompt=prompt)
        self.wait_for(lambda: self.status()["dead"])
        output = self.output()
        self.assertEqual(output["prompt"], prompt)
        self.assertEqual(output["argv"], [prompt])
        self.assertEqual(output["cwd"], str(self.ui))

    def test_no_prompt_adds_no_task_arguments(self):
        for agent in ("pi", "claude"):
            self.start(agent=agent)
            self.wait_for(lambda: self.status()["dead"])
            self.assertEqual(self.output()["argv"], [])
            self.worker.dispatch({"action": "cancel", "name": "job"})

    def test_lifetime_survives_request_and_cancel_removes_session(self):
        requester = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.util,json,sys; s=importlib.util.spec_from_file_location('worker',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.Worker(sys.argv[2],json.loads(sys.argv[3]),scope=[]).dispatch(json.load(sys.stdin))))",
                str(ROOT / "remote-agent-worker.py"),
                str(self.home),
                json.dumps(self.tmux),
            ],
            input=json.dumps(
                {
                    "action": "start",
                    "name": "job",
                    "repo": "engine",
                    "agent": "pi",
                    "prompt": "hold",
                }
            ),
            text=True,
            capture_output=True,
        )
        self.assertEqual(requester.returncode, 0, requester.stderr)
        self.output()
        other = self.module.Worker(self.home, self.tmux, scope=[])
        self.assertFalse(other.dispatch({"action": "status", "name": "job"})["dead"])
        with self.assertRaisesRegex(
            (ValueError, RuntimeError, FileExistsError), "exist"
        ):
            self.start(prompt="hold")
        other.dispatch({"action": "cancel", "name": "job"})
        with self.assertRaisesRegex(
            (ValueError, RuntimeError), "session|find|server|No such file"
        ):
            self.status()
        self.start(prompt="hold")
        self.output()

    def test_invalid_names_and_foreign_session_are_untouched(self):
        subprocess.run(
            self.tmux
            + [
                "new-session",
                "-d",
                "-s",
                "foreign",
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
            check=True,
        )
        for name in ("../bad", "foreign:0", "-x", "bad name", "", "a.b", "a" * 65):
            for action in ("start", "status", "logs", "cancel", "attach"):
                with self.assertRaisesRegex(ValueError, "name"):
                    self.worker.dispatch(
                        {
                            "action": action,
                            "name": name,
                            "repo": "engine",
                            "agent": "pi",
                        }
                    )
        with self.assertRaises((ValueError, RuntimeError)):
            self.worker.dispatch({"action": "cancel", "name": "foreign"})
        self.assertEqual(
            subprocess.run(self.tmux + ["has-session", "-t", "=foreign"]).returncode, 0
        )

    def test_unexpected_session_directory_is_rejected(self):
        self.start()
        self.wait_for(lambda: self.status()["dead"])
        with self.assertRaisesRegex(
            (ValueError, RuntimeError, FileExistsError), "exist"
        ):
            self.start()

    def test_bad_request_missing_repo_and_executable(self):
        for request in (
            {},
            [],
            {"action": "start", "name": "job", "repo": "other", "agent": "pi"},
            {"action": "start", "name": "job", "repo": "engine", "agent": "bad"},
            {
                "action": "start",
                "name": "job",
                "repo": "engine",
                "agent": "pi",
                "prompt": 3,
            },
        ):
            with self.assertRaises(ValueError):
                self.worker.dispatch(request)
        self.engine.rmdir()
        with self.assertRaisesRegex((ValueError, FileNotFoundError), "repo|directory"):
            self.start()
        self.engine.mkdir()
        (self.home / ".local/bin/i").unlink()
        with self.assertRaisesRegex((ValueError, FileNotFoundError), "executable|i"):
            self.start(agent="claude")

    def test_worker_attach_to_unknown_session_exits_refused(self):
        env = dict(os.environ, HOME=str(self.home))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "remote-agent-worker.py"),
                "--attach",
                "nosuch",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("no such session", result.stderr)

    def test_symlink_state_and_unexpected_cancel_files_are_rejected(self):
        state = self.home / ".local/state"
        state.mkdir()
        outside = self.home / "outside"
        outside.mkdir()
        (state / "remote-agent").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unexpected filesystem"):
            self.start()
        self.assertEqual(list(outside.iterdir()), [])
        (state / "remote-agent").unlink()
        self.start(prompt="hold")
        self.output()
        directory = state / "remote-agent/remote-agent-job"
        (directory / "unexpected").write_text("keep me")
        self.addCleanup(lambda: (directory / "unexpected").unlink(missing_ok=True))
        with self.assertRaisesRegex(ValueError, "unexpected filesystem"):
            self.worker.dispatch({"action": "cancel", "name": "job"})
        self.assertEqual((directory / "unexpected").read_text(), "keep me")
        (directory / "unexpected").unlink()
        self.assertFalse(self.status()["dead"])

    def test_dtach_launch_failure_cleans_only_created_state(self):
        binary = self.home / ".local/bin/detach"
        binary.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
        binary.chmod(0o700)
        with self.assertRaisesRegex(RuntimeError, "dtach startup failed"):
            self.worker.dispatch(
                {
                    "action": "start",
                    "name": "job",
                    "repo": "engine",
                    "agent": "pi",
                    "prompt": "hold",
                }
            )
        self.assertFalse(
            (self.home / ".local/state/remote-agent/remote-agent-job").exists()
        )
        binary.unlink()
        self.start()
        self.wait_for(lambda: self.status()["dead"])

    def test_non_string_selectors_are_request_errors(self):
        for field in ("repo", "agent"):
            request = {
                "action": "start",
                "name": "job",
                "repo": "engine",
                "agent": "pi",
            }
            request[field] = []
            with self.assertRaises(ValueError):
                self.worker.dispatch(request)

    def test_projects_lists_map_and_unknown_project_names_choices(self):
        projects = FIXTURE_PROJECTS
        self.assertEqual(projects["ui-demo"]["remote"], "Projects/demo/ui-demo")
        self.assertEqual(
            self.worker.dispatch({"action": "projects"}), {"projects": projects}
        )
        with self.assertRaisesRegex(ValueError, ", ".join(projects)):
            self.start(repo="other")

    def test_list_reports_only_remote_agent_sessions_with_project_and_harness(self):
        self.assertEqual(self.worker.dispatch({"action": "list"}), {"sessions": []})
        subprocess.run(
            self.tmux
            + [
                "new-session",
                "-d",
                "-s",
                "foreign",
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ],
            check=True,
        )
        self.start(name="eng", agent="pi", prompt="hold")
        self.start(name="ui", repo="ui-demo", agent="claude")
        self.wait_for(lambda: self.status("ui")["dead"])
        sessions = sorted(
            self.worker.dispatch({"action": "list"})["sessions"],
            key=lambda item: item["name"],
        )
        self.assertEqual(
            sessions,
            [
                {
                    "name": "eng",
                    "project": "engine",
                    "harness": "pi",
                    "backend": "dtach",
                    "attached": False,
                    "dead": False,
                    "exit_status": None,
                },
                {
                    "name": "ui",
                    "project": "ui-demo",
                    "harness": "claude",
                    "backend": "dtach",
                    "attached": False,
                    "dead": True,
                    "exit_status": 7,
                },
            ],
        )

    def test_worker_cli_reports_invalid_json(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "remote-agent-worker.py")],
            input="not JSON",
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error", json.loads(result.stdout))


class LegacyTmuxWorkerTests(WorkerFixture):
    def start(self, name="job", repo="engine", agent="pi", prompt=None, resume=None):
        """Recreate an old-format record and launch its original tmux session."""
        self.observation.unlink(missing_ok=True)
        session = "remote-agent-" + name
        directory = self.worker.create_directory(session)
        conversation = resume or "legacy-conversation"
        self.module.write_private(
            directory / "session.json",
            json.dumps({"repo": repo, "agent": agent, "conversation": conversation}),
        )
        prompt_file = None
        if prompt is not None:
            prompt_file = directory / "prompt.txt"
            self.module.write_private(prompt_file, prompt)
        flag = self.module.RESUME_FLAGS[agent] if resume else self.module.NEW_FLAG
        self.worker.launch(session, repo, agent, [flag, conversation], prompt_file)

    def test_legacy_logs_retain_agent_output(self):
        self.start(prompt="legacy output")
        self.wait_for(lambda: self.status()["dead"])
        output = self.output()
        captured = [
            json.loads(line)
            for line in self.logs().splitlines()
            if line.startswith("{")
        ]
        self.assertEqual(captured, [output])

    def test_cancel_cleans_state_left_by_a_killed_tmux_server(self):
        self.start(prompt="hold")
        self.output()
        subprocess.run(self.tmux + ["kill-server"], check=True)  # e.g. WSL shutdown
        self.assertEqual(
            self.worker.dispatch({"action": "cancel", "name": "job"}),
            {"session": "remote-agent-job", "cancelled": True},
        )
        self.start(prompt="hold")
        self.output()
        with self.assertRaises((ValueError, RuntimeError)):
            self.worker.dispatch({"action": "cancel", "name": "never-started"})

    def test_attach_real_terminal_then_detach_preserves_agent(self):
        self.start(prompt="hold")
        self.output()
        self.attach_and_detach()
        self.assertFalse(self.status()["dead"])

    def attach_and_detach(self, name="job"):
        """Attach on a real terminal, wait for the agent's output line, detach."""
        pid, terminal = pty.fork()
        if pid == 0:
            os.environ["TERM"] = "xterm"
            self.worker.dispatch({"action": "attach", "name": name})
            os._exit(1)
        try:
            content = b""
            deadline = time.monotonic() + 5
            while b'{"argv"' not in content and time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    content += os.read(terminal, 65536)
            self.assertIn(b'{"argv"', content)
            os.write(terminal, b"\x02d")
            result = self.wait_for(
                lambda: value if (value := os.waitpid(pid, os.WNOHANG))[0] else None
            )
            self.assertEqual(result, (pid, 0))
            pid = None
        finally:
            if pid is not None:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(terminal)

    def test_attach_after_server_death_resumes_recorded_conversation(self):
        for agent, flag in (("claude", "--resume"), ("pi", "--session")):
            self.start(agent=agent, prompt="hold")
            created = self.output()
            self.assertEqual(created["conversation"][0], "--session-id")
            conversation = created["conversation"][1]
            subprocess.run(self.tmux + ["kill-server"], check=True)  # WSL restart
            self.observation.unlink()
            self.attach_and_detach()
            resumed = self.output()
            self.assertEqual(resumed["executable"], "i" if agent == "claude" else "pi")
            self.assertEqual(resumed["conversation"], [flag, conversation])
            self.assertIsNone(resumed["prompt"])  # task prompt is not resent
            self.worker.dispatch({"action": "cancel", "name": "job"})

    def test_attach_after_server_death_resumes_explicit_resume_id(self):
        self.start(agent="claude", resume="f86e1850-973d")
        self.wait_for(lambda: self.status()["dead"])
        subprocess.run(self.tmux + ["kill-server"], check=True)
        self.restart()
        self.wait_for(lambda: self.status()["dead"])
        self.assertEqual(self.output()["conversation"], ["--resume", "f86e1850-973d"])


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
# Claude Code 2.1.288 Linux clipboard commands (from the binary), verbatim.
CLAUDE_CHECK = 'xclip -selection clipboard -t TARGETS -o 2>/dev/null | grep -E "image/(png|jpeg|jpg|gif|webp|bmp)" || wl-paste -l 2>/dev/null | grep -E "image/(png|jpeg|jpg|gif|webp|bmp)"'
CLAUDE_SAVE = "xclip -selection clipboard -t image/png -o > {0} 2>/dev/null || wl-paste --type image/png > {0} 2>/dev/null || xclip -selection clipboard -t image/bmp -o > {0} 2>/dev/null || wl-paste --type image/bmp > {0}"


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        isolate_home(self, self.home)
        self.module = load_worker()
        self.bindir = self.home / ".local/bin"
        self.bindir.mkdir(parents=True)
        (self.bindir / "wl-paste").symlink_to(ROOT / "remote-agent-worker.py")
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bindir}:{Path(sys.executable).parent}:/usr/bin:/bin",
        }

    def claude_shell(self, command):
        return subprocess.run(["sh", "-c", command], env=self.env, capture_output=True)

    def test_claude_ctrl_v_reads_uploaded_png(self):
        self.assertNotEqual(self.claude_shell(CLAUDE_CHECK).returncode, 0)
        worker = self.module.Worker(self.home)
        worker.dispatch({"action": "clipboard", "png": base64.b64encode(PNG).decode()})
        stored = self.home / ".local/state/remote-agent-clipboard.png"
        self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
        check = self.claude_shell(CLAUDE_CHECK)
        self.assertEqual((check.returncode, check.stdout), (0, b"image/png\n"))
        target = self.home / "pasted.png"
        save = self.claude_shell(CLAUDE_SAVE.format(target))
        self.assertEqual(save.returncode, 0, save.stderr)
        self.assertEqual(target.read_bytes(), PNG)

    def test_pi_reads_exact_uploaded_png(self):
        worker = self.module.Worker(self.home)
        worker.dispatch({"action": "clipboard", "png": base64.b64encode(PNG).decode()})
        for args, expected in (
            (["--list-types"], b"image/png\n"),
            (["--type", "image/png", "--no-newline"], PNG),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(self.bindir / "wl-paste"), *args],
                    env=self.env,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, b"")

    def test_supported_calls_fail_without_uploaded_image(self):
        for args in (
            ["-l"],
            ["--list-types"],
            ["--type", "image/png"],
            ["--type", "image/png", "--no-newline"],
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(self.bindir / "wl-paste"), *args],
                    env=self.env,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(
                    result.stderr,
                    b"No image uploaded; run `remote-agent paste` on the laptop.\n",
                )

    def test_rejects_non_png_upload(self):
        worker = self.module.Worker(self.home)
        for value in (base64.b64encode(b"GIF89a").decode(), "not base64!", 3):
            with self.assertRaises(ValueError):
                worker.dispatch({"action": "clipboard", "png": value})
        self.assertFalse(
            (self.home / ".local/state/remote-agent-clipboard.png").exists()
        )


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.local_home = self.home / "controller-home"
        self.config = write_config(self.local_home)
        isolate_home(self, self.local_home)
        os.environ.pop("PI_CODING_AGENT_DIR", None)
        (self.local_home / ".local/bin").mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("deploy", ROOT / "deploy.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.bin = self.home / "tools"
        self.bin.mkdir()
        for program in ("systemctl", "tmux"):
            executable = self.bin / program
            executable.write_text(f"""#!{sys.executable}
import json,os,sys
with open(os.environ['CALLS'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if {program!r} == 'tmux': sys.exit(1)
if 'show' in sys.argv:
    print(os.environ.get('USER_SLICE' if '--user' in sys.argv else 'SYSTEM_SLICE', ''))
""")
            executable.chmod(0o700)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "CALLS": str(self.home / "calls"),
        }

    def deploy(self, archive=None, **env):
        request = {
            "archive": self.module.bundle_files() if archive is None else archive,
            "config": {
                name: base64.b64encode((self.config / name).read_bytes()).decode()
                for name in ("remote-hosts.json", "remote-projects.json")
            },
            "revision": "fixture-revision",
            "home": str(self.home),
            "host": "agent-server",
        }
        return subprocess.run(
            [sys.executable, "-c", self.module.REMOTE_PROGRAM],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            env={**self.env, **env},
        )

    def test_deployed_config_is_private_and_used_by_worker_and_extension(self):
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True, mode=0o755)
        for name in ("remote-hosts.json", "remote-projects.json"):
            path = config / name
            path.write_text("{}")
            path.chmod(0o644)
        # Preserve byte representation, not merely equivalent parsed JSON.
        projects = self.config / "remote-projects.json"
        projects.write_text(json.dumps(FIXTURE_PROJECTS, indent=2) + "\n")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o700)
        installed = self.home / "Projects/cli/remote-agent"
        for name in ("remote-hosts.json", "remote-projects.json"):
            self.assertEqual(
                (config / name).read_bytes(), (self.config / name).read_bytes()
            )
            self.assertEqual(stat.S_IMODE((config / name).stat().st_mode), 0o600)
            self.assertFalse((installed / name).exists())
        worker = subprocess.run(
            [
                sys.executable,
                str(installed / "remote-agent-worker.py"),
                "--host",
                "agent-server",
            ],
            input=json.dumps({"action": "projects"}),
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(worker.returncode, 0, worker.stderr)
        self.assertEqual(json.loads(worker.stdout), {"projects": FIXTURE_PROJECTS})
        extension = subprocess.run(
            ["node", "--test", str(installed / "tests/test_periodic_scheduler.mjs")],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(extension.returncode, 0, extension.stdout + extension.stderr)

    def test_bundle_contains_generic_extension_without_project_checklist(self):
        archive = base64.b64decode(self.module.bundle_files())
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            names = bundle.getnames()
            self.assertIn("periodic-checks.ts", names)
            for helper in (
                "tests/periodic_scheduler_dispatch.mjs",
                "tests/periodic_scheduler_fixture.mjs",
                "tests/test_periodic_runtime.py",
                "tests/fixtures/periodic-runtime-provider.ts",
            ):
                self.assertIn(helper, names)
                with bundle.extractfile(helper) as content:
                    self.assertEqual(content.read(), (ROOT / helper).read_bytes())
            self.assertEqual(
                [name for name in names if name.endswith(".ts")],
                ["periodic-checks.ts", "tests/fixtures/periodic-runtime-provider.ts"],
            )
            self.assertEqual(
                [name for name in names if name.endswith(".md")], ["README.md"]
            )
            with bundle.extractfile("periodic-checks.ts") as content:
                self.assertEqual(
                    content.read(), (ROOT / "periodic-checks.ts").read_bytes()
                )

    def install_extension(self, route, agent_dir=None):
        if route == "local":
            env = {} if agent_dir is None else {"PI_CODING_AGENT_DIR": str(agent_dir)}
            with mock.patch.dict(os.environ, env):
                self.module.link_controller()
        else:
            env = {} if agent_dir is None else {"PI_CODING_AGENT_DIR": str(agent_dir)}
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as bundle:
                for name in ("tmux.conf", "agents.slice", "remote-agent-worker.py"):
                    bundle.add(ROOT / name, arcname=name)
                extension = b"export default function () {}\n"
                entry = tarfile.TarInfo("periodic-checks.ts")
                entry.size = len(extension)
                bundle.addfile(entry, io.BytesIO(extension))
            result = self.deploy(
                archive=base64.b64encode(archive.getvalue()).decode(), **env
            )
            if result.returncode:
                raise RuntimeError(result.stderr)

    def extension_paths(self, route):
        home = self.local_home if route == "local" else self.home
        source = (
            ROOT if route == "local" else home / "Projects/cli/remote-agent"
        ) / "periodic-checks.ts"
        return home / ".config/pi/agent/extensions/periodic-checks.ts", source

    def test_user_extension_link_created_and_idempotent_in_both_routes(self):
        for route in ("local", "remote"):
            with self.subTest(route=route):
                link, source = self.extension_paths(route)
                self.install_extension(route)
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.readlink(), source)
                inode = link.lstat().st_ino
                self.install_extension(route)
                self.assertEqual(link.lstat().st_ino, inode)
                self.assertEqual(link.read_bytes(), source.read_bytes())

    def test_user_extension_honors_pi_agent_dir_in_both_routes(self):
        for route in ("local", "remote"):
            with self.subTest(route=route):
                default, source = self.extension_paths(route)
                directory = default.parents[3] / "custom-agent"
                self.install_extension(route, directory)
                link = directory / "extensions/periodic-checks.ts"
                self.assertEqual(link.readlink(), source)
                self.assertFalse(default.exists())

    def test_user_extension_refuses_unmanaged_file_in_both_routes(self):
        for route in ("local", "remote"):
            with self.subTest(route=route):
                link, _ = self.extension_paths(route)
                link.parent.mkdir(parents=True)
                link.write_text("user extension")
                with self.assertRaisesRegex(
                    (RuntimeError, FileExistsError), "unmanaged"
                ):
                    self.install_extension(route)
                self.assertFalse(link.is_symlink())
                self.assertEqual(link.read_text(), "user extension")

    def test_user_extension_refuses_foreign_dangling_link_in_both_routes(self):
        for route in ("local", "remote"):
            with self.subTest(route=route):
                link, _ = self.extension_paths(route)
                link.parent.mkdir(parents=True)
                foreign = self.home / "foreign-extension.ts"
                link.symlink_to(foreign)
                with self.assertRaisesRegex((RuntimeError, FileExistsError), "foreign"):
                    self.install_extension(route)
                self.assertEqual(link.readlink(), foreign)

    def test_deploy_preserves_existing_tmux_configuration(self):
        installed = self.home / "Projects/cli/remote-agent"
        installed.mkdir(parents=True)
        target = installed / "tmux.conf"
        config = self.home / ".tmux.conf"
        content = "set -g mouse off\nset -ga terminal-overrides ',*:smcup@:rmcup@'\n"
        for symlink in (True, False):
            with self.subTest(symlink=symlink):
                config.unlink(missing_ok=True)
                target.write_text(content)
                if symlink:
                    config.symlink_to(target)
                else:
                    config.write_text(content)
                result = self.deploy()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config.read_text(), content)
                self.assertEqual(target.read_text(), content)
                self.assertEqual(config.is_symlink(), symlink)
                config.unlink()

    def test_preserve_user_slice_and_install_native_worker(self):
        unit = self.home / ".config/systemd/user/agents.slice"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Slice]\nMemoryMax=42G\n")
        result = self.deploy(USER_SLICE=str(unit))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unit.read_text(), "[Slice]\nMemoryMax=42G\n")
        installed = self.home / "Projects/cli/remote-agent"
        self.assertEqual(
            (installed / "remote-agent-worker.py").read_bytes(),
            (ROOT / "remote-agent-worker.py").read_bytes(),
        )
        self.assertEqual(
            json.loads(
                (self.home / ".config/remote-agent/remote-hosts.json").read_text()
            )["agent-server"]["cargo_build_jobs"],
            "20",
        )
        self.assertEqual(
            (self.home / "data/remote-agents/controller-revision.txt").read_text(),
            "fixture-revision\n",
        )
        self.assertFalse((self.home / ".tmux.conf").exists())

    def test_deploy_cli_defaults_to_native_and_preserves_desktop_override(self):
        ssh = self.bin / "ssh"
        ssh.write_text(
            f'#!{sys.executable}\nimport json,sys\nprint(json.dumps({{"argv":sys.argv[1:],"request":json.load(sys.stdin)}}))\n'
        )
        ssh.chmod(0o700)
        write_config(self.home)
        (self.home / ".local/bin").mkdir(parents=True)
        for args, target, home, windows in (
            ((), "agent-server", "/home/fixture", False),
            (("--host", "desktop"), "desktop", "/home/fixture-test", True),
            (("--host", "agent-server"), "agent-server", "/home/fixture", False),
        ):
            result = subprocess.run(
                [str(ROOT / "deploy.sh"), *args],
                env=self.env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["argv"][-2], target)
            self.assertEqual("wsl.exe" in data["argv"][-1], windows)
            self.assertEqual(data["request"]["home"], home)
            for name in ("remote-hosts.json", "remote-projects.json"):
                self.assertEqual(
                    base64.b64decode(data["request"]["config"][name]),
                    (self.home / ".config/remote-agent" / name).read_bytes(),
                )
            self.assertEqual(
                (self.home / ".local/bin/remote-agent").resolve(),
                ROOT / "remote-agent.py",
            )

    def test_system_slice_is_not_shadowed(self):
        result = self.deploy(SYSTEM_SLICE="/etc/systemd/system/agents.slice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".config/systemd/user/agents.slice").exists())

    def test_install_slice_only_when_no_host_slice_exists(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.home / ".config/systemd/user/agents.slice").read_bytes(),
            (ROOT / "agents.slice").read_bytes(),
        )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            (ROOT / "remote-agent.py").exists(), "local controller is not implemented"
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        isolate_home(self, self.root)
        wl_paste = self.root / "wl-paste"
        wl_paste.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
        wl_paste.chmod(0o700)
        ssh = self.root / "ssh"
        ssh.write_text(f"""#!{sys.executable}
import json, pathlib, sys
argv = sys.argv[1:]
with pathlib.Path({str(self.root / "ssh-calls")!r}).open('a') as calls:
    calls.write(json.dumps(argv) + '\\n')
request = None if "-t" in argv else json.load(sys.stdin)
names = ['job', 'shared'] if 'desktop' in argv else ['ui-demo', 'custom', 'shared']
name = argv[-1] if '--attach' in argv else ((request or {{}}).get('name') if (request or {{}}).get('action') in ('locate', 'status', 'logs', 'cancel') else None)
if name is not None and name not in names:
    print('no such session: ' + name, file=sys.stderr)
    sys.exit(3 if '--attach' in argv else 1)
if request and request['action'] == 'locate':
    print(json.dumps({{'exists': name in names}}))
elif request and request['action'] == 'list':
    print(json.dumps({{'sessions': [{{'name': 'job' if 'desktop' in argv else 'custom'}}]}}))
elif request and request['action'] == 'history':
    print(json.dumps({{'available': True, 'history': '', 'project': 'fixture'}}))
else:
    print(json.dumps({{"argv": argv, "request": request}}))
""")
        ssh.chmod(0o700)
        mosh = self.root / "mosh"
        mosh.write_text(
            ssh.read_text()
            .replace("ssh-calls", "mosh-calls")
            .replace(
                'request = None if "-t" in argv else json.load(sys.stdin)',
                "request = None",
            )
            .replace("sys.exit(3 if '--attach' in argv else 1)", "sys.exit(0)")
        )
        mosh.chmod(0o700)
        self.env = dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ["PATH"],
            COLORTERM="truecolor",
        )

    def invoke(self, *args, platform=None):
        command = [sys.executable, str(ROOT / "remote-agent.py"), *args]
        if platform is not None:
            command = [
                sys.executable,
                "-c",
                "import runpy, sys; "
                f"sys.platform = {platform!r}; "
                "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')",
                str(ROOT / "remote-agent.py"),
                *args,
            ]
        return subprocess.run(
            command,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=3,
        )

    def test_json_prompt_transport_and_fixed_destination(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("hello\n; ' $ é")
        result = self.invoke(
            "start",
            "engine",
            "job",
            "--agent-harness",
            "pi",
            "--prompt-file",
            str(prompt),
            "--host",
            "desktop",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(
            data["request"],
            {
                "action": "start",
                "repo": "engine",
                "agent": "pi",
                "name": "job",
                "prompt": prompt.read_text(),
                "colorterm": "truecolor",
            },
        )
        self.assertEqual(
            data["argv"],
            [
                "desktop",
                "wsl.exe",
                "-d",
                "FixtureBuild",
                "-u",
                "fixture-test",
                "--exec",
                "python3",
                "/home/fixture-test/Projects/cli/remote-agent/remote-agent-worker.py",
            ],
        )

    def test_start_defaults_to_pi_and_project_name(self):
        result = self.invoke("start", "ui-demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["request"],
            {
                "action": "start",
                "name": "ui-demo",
                "repo": "ui-demo",
                "agent": "pi",
                "colorterm": "truecolor",
            },
        )
        self.assertNotEqual(
            self.invoke("start", "ui-demo", "--agent-harness", "codex").returncode, 0
        )

    def test_start_resume_is_sent_to_worker(self):
        result = self.invoke("start", "engine", "lead", "--resume", "4f18858a-d8a1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["request"],
            {
                "action": "start",
                "name": "lead",
                "repo": "engine",
                "agent": "pi",
                "resume": "4f18858a-d8a1",
                "colorterm": "truecolor",
            },
        )

    def test_projects_lists_paths_without_routing_hosts(self):
        result = self.invoke("projects")
        self.assertEqual(result.returncode, 0, result.stderr)
        projects = json.loads(result.stdout)["projects"]
        self.assertEqual(set(projects), {"engine", "server", "ui-demo"})
        for project in projects.values():
            self.assertEqual(set(project), {"local", "remote"})

    def test_list_queries_only_selected_host(self):
        for options, host, name in (
            ((), "agent-server", "custom"),
            (("--host", "desktop"), "desktop", "job"),
        ):
            with self.subTest(host=host):
                (self.root / "ssh-calls").unlink(missing_ok=True)
                result = self.invoke("list", *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {"sessions": [{"name": name, "host": host}], "errors": []},
                )
                calls = (self.root / "ssh-calls").read_text().splitlines()
                self.assertEqual(len(calls), 1)
                self.assertIn(host, json.loads(calls[0]))

    def test_start_and_attach_use_same_default_and_explicit_hosts(self):
        for options, host in (
            ((), "agent-server"),
            (("--host", "desktop"), "desktop"),
        ):
            for project in ("engine", "ui-demo"):
                with self.subTest(host=host, project=project):
                    for action, args in (
                        ("start", (project, "shared")),
                        ("attach", ("shared",)),
                    ):
                        result = self.invoke(action, *args, *options)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        argv = json.loads(result.stdout)["argv"]
                        self.assertIn(host, argv)
                        if action == "attach":
                            self.assertEqual(argv[:3], ["--no-init", "--", host])
                            self.assertEqual(argv[-2:], ["--attach", "shared"])
                            self.assertNotIn("-t", argv)
                            self.assertNotIn("-o", argv)
                            self.assertEqual("wsl.exe" in argv, host == "desktop")

    def test_start_unknown_project_fails_before_ssh(self):
        result = self.invoke("start", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project must be one of:", result.stderr)
        self.assertFalse((self.root / "ssh-calls").exists())

    def test_unavailable_desktop_does_not_block_native_session(self):
        ssh = self.root / "ssh"
        script = ssh.read_text()
        script = script.replace(
            "argv = sys.argv[1:]",
            "argv = sys.argv[1:]\nif 'desktop' in argv:\n    print('old desktop worker', file=sys.stderr)\n    sys.exit(1)",
        )
        ssh.write_text(script)
        result = self.invoke("attach", "ui-demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("lookup unavailable", result.stderr)
        self.assertIn("agent-server", json.loads(result.stdout)["argv"])
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        listing = json.loads(result.stdout)
        self.assertEqual(
            listing["sessions"], [{"name": "custom", "host": "agent-server"}]
        )
        self.assertEqual(listing["errors"], [])
        result = self.invoke("list", "--host", "desktop")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["sessions"], [])

    def test_attach_default_never_probes_or_retries_unreachable_desktop(self):
        ssh = self.root / "ssh"
        ssh.write_text(
            ssh.read_text().replace(
                'request = None if "-t" in argv else json.load(sys.stdin)',
                "if 'desktop' in argv:\n    print('desktop SSH unavailable', file=sys.stderr)\n    sys.exit(255)\nrequest = None if \"-t\" in argv else json.load(sys.stdin)",
            )
        )
        try:
            result = self.invoke("attach", "ui-demo")
        except subprocess.TimeoutExpired:
            self.fail("default attach stalled probing/retrying unreachable desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            json.loads(line)
            for line in (self.root / "ssh-calls").read_text().splitlines()
        ]
        self.assertEqual(len(calls), 1)
        self.assertIn("agent-server", calls[0])
        self.assertNotIn("desktop", calls[0])
        self.assertNotIn("retrying", result.stderr)
        self.assertNotIn("reconnecting", result.stderr)
        mosh_calls = [
            json.loads(line)
            for line in (self.root / "mosh-calls").read_text().splitlines()
        ]
        self.assertEqual(len(mosh_calls), 1)
        self.assertEqual(mosh_calls[0][:3], ["--no-init", "--", "agent-server"])

    def test_session_commands_route_directly_to_selected_worker(self):
        for action in ("status", "logs", "attach", "cancel"):
            for name in ("job", "shared", "custom"):
                for options, host in (
                    ((), "agent-server"),
                    (("--host", "desktop"), "desktop"),
                ):
                    with self.subTest(action=action, name=name, host=host):
                        (self.root / "ssh-calls").unlink(missing_ok=True)
                        result = self.invoke(action, name, *options)
                        exists = name in (
                            ("job", "shared")
                            if host == "desktop"
                            else ("shared", "custom")
                        )
                        self.assertEqual(
                            result.returncode, 0 if exists else 1, result.stderr
                        )
                        if not exists:
                            self.assertIn("no such session", result.stderr)
                        calls = [
                            json.loads(line)
                            for line in (self.root / "ssh-calls")
                            .read_text()
                            .splitlines()
                        ]
                        self.assertEqual(len(calls), 1)
                        self.assertIn(host, calls[0])
                        self.assertNotIn(
                            "desktop" if host == "agent-server" else "agent-server",
                            calls[0],
                        )

    def test_native_start_and_direct_session_routing(self):
        for command in [
            ("start", "ui-demo"),
            ("status", "custom"),
            ("logs", "custom"),
            ("cancel", "custom"),
            ("attach", "ui-demo"),
        ]:
            result = self.invoke(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(result.stdout)["argv"]
            self.assertIn("agent-server", argv)
            self.assertIn(
                "/home/fixture/Projects/cli/remote-agent/remote-agent-worker.py", argv
            )
            self.assertNotIn("wsl.exe", argv)
            self.assertIn("--host", argv)

    def test_native_login_attach_and_ip_override(self):
        self.env["REMOTE_AGENT_AGENT_SERVER_SSH"] = "fixture@192.0.2.10"
        for command in [
            ("pi-login", "openai-codex", "--host", "agent-server"),
            ("claude-account", "--host", "agent-server"),
            ("attach", "custom", "--host", "agent-server"),
        ]:
            result = self.invoke(*command)
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = json.loads(result.stdout)["argv"]
            self.assertIn("fixture@192.0.2.10", argv)
            self.assertNotIn("wsl.exe", argv)

    def test_auth_commands_preserve_explicit_desktop_host(self):
        for command in (
            ("claude-account",),
            ("claude-account", "--login", "work"),
            ("pi-login", "openai-codex"),
        ):
            with self.subTest(command=command):
                result = self.invoke(*command, "--host", "desktop")
                self.assertEqual(result.returncode, 0, result.stderr)
                for line in result.stdout.splitlines():
                    argv = json.loads(line)["argv"]
                    self.assertIn("desktop", argv)
                    self.assertNotIn("agent-server", argv)

    def test_unknown_session_is_refused_by_selected_worker(self):
        for action in ("attach", "cancel"):
            result = self.invoke(action, "missing")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no such session", result.stderr)

    def test_windows_title_trailer_is_accepted_only_on_desktop(self):
        ssh = self.root / "ssh"
        ssh.write_text(f"""#!{sys.executable}
import json,sys
json.load(sys.stdin)
print(json.dumps({{'sessions': [{{'name': 'job'}}]}}))
sys.stdout.write('\\x1b]2;Terminal\\x07')
""")
        result = self.invoke("list")
        self.assertNotEqual(result.returncode, 0)
        listing = json.loads(result.stdout)
        self.assertEqual(listing["sessions"], [])
        self.assertEqual(len(listing["errors"]), 1)
        result = self.invoke("list", "--host", "desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["sessions"], [{"name": "job", "host": "desktop"}]
        )

    def test_paste_sends_laptop_clipboard_png(self):
        wl_paste = self.root / "wl-paste"
        wl_paste.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({PNG!r})\n"
        )
        wl_paste.chmod(0o700)
        for options, host in (
            ((), "agent-server"),
            (("--host", "desktop"), "desktop"),
            (("--host", "agent-server"), "agent-server"),
        ):
            with self.subTest(host=host, options=options):
                result = self.invoke("paste", *options, platform="linux")
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["argv"][0], host)
                request = data["request"]
                self.assertEqual(request["action"], "clipboard")
                self.assertEqual(base64.b64decode(request["png"]), PNG)

    def test_macos_paste_sends_png_to_selected_host(self):
        osascript = self.root / "osascript"
        osascript.write_text(
            f"#!{sys.executable}\nprint({base64.b64encode(PNG).decode()!r})\n"
        )
        osascript.chmod(0o700)
        for options, host in (((), "agent-server"), (("--host", "desktop"), "desktop")):
            with self.subTest(host=host):
                result = self.invoke("paste", *options, platform="darwin")
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data["argv"][0], host)
                self.assertEqual(data["request"]["action"], "clipboard")
                self.assertEqual(base64.b64decode(data["request"]["png"]), PNG)

    def test_macos_clipboard_failure_does_not_upload(self):
        osascript = self.root / "osascript"
        for output, error, code in (
            ("", "clipboard has no image", 1),
            ("", "", 0),
            ("", "clipboard access denied", 1),
            ("not base64!", "", 0),
        ):
            with self.subTest(output=output, error=error, code=code):
                osascript.write_text(
                    f"#!{sys.executable}\nimport sys\n"
                    f"sys.stdout.write({output!r})\n"
                    f"sys.stderr.write({error!r})\nsys.exit({code})\n"
                )
                osascript.chmod(0o700)
                result = self.invoke("paste", platform="darwin")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("clipboard", result.stderr)
                if error:
                    self.assertIn(error, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.root / "ssh-calls").exists())

    def test_paste_without_image_fails_before_ssh(self):
        wl_paste = self.root / "wl-paste"
        wl_paste.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
        wl_paste.chmod(0o700)
        result = self.invoke("paste", platform="linux")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no PNG image", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_claude_account_requests(self):
        for args, expected in (
            ((), {"action": "claude-account"}),
            (("gc",), {"action": "claude-account", "use": "gc"}),
            (
                ("--name-current", "personal"),
                {"action": "claude-account", "name_current": "personal"},
            ),
        ):
            result = self.invoke("claude-account", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["request"], expected)
            self.assertIn("agent-server", data["argv"])
            self.assertNotIn("desktop", data["argv"])

    def test_claude_account_login_prepares_named_directory_then_selects(self):
        result = self.invoke("claude-account", "--login", "work")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(
            calls[0]["request"], {"action": "claude-account", "prepare_login": "work"}
        )
        self.assertEqual(calls[1]["argv"][0], "-t")
        self.assertEqual(calls[1]["argv"][-2:], ["--claude-login", "work"])
        for call in calls:
            self.assertIn("agent-server", call["argv"])
            self.assertNotIn("desktop", call["argv"])
        self.assertEqual(
            calls[2]["request"], {"action": "claude-account", "use": "work"}
        )

    def test_pi_login_runs_on_tty_with_validated_provider(self):
        result = self.invoke("pi-login", "openai-codex-gc")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["argv"][0], "-t")
        self.assertEqual(data["argv"][-2:], ["--pi-login", "openai-codex-gc"])
        self.assertIn("agent-server", data["argv"])
        self.assertNotIn("desktop", data["argv"])
        self.assertNotEqual(self.invoke("pi-login", "x&calc").returncode, 0)

    def test_attach_uses_mosh_and_validated_name_only(self):
        result = self.invoke("attach", "custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(
            data["argv"],
            [
                "--no-init",
                "--",
                "agent-server",
                "python3",
                "/home/fixture/Projects/cli/remote-agent/remote-agent-worker.py",
                "--host",
                "agent-server",
                "--colorterm=truecolor",
                "--attach",
                "custom",
            ],
        )
        self.assertTrue((self.root / "mosh-calls").exists())
        self.assertIsNone(data["request"])
        self.assertNotEqual(self.invoke("attach", "job;evil").returncode, 0)

    def attach_with_mosh_exits(self, *codes, session_exists=True):
        """Run attach on a real terminal; return status, output and Mosh argv."""
        if session_exists:
            ssh = self.root / "ssh"
            ssh.write_text(
                ssh.read_text().replace(
                    "['ui-demo', 'custom', 'shared']",
                    "['job', 'ui-demo', 'custom', 'shared']",
                )
            )
        calls = self.root / "calls"
        mosh = self.root / "mosh"
        mosh.write_text(f"""#!{sys.executable}
import json, pathlib, sys
calls = pathlib.Path({str(calls)!r})
done = calls.read_text().splitlines() if calls.exists() else []
calls.write_text("".join(line + "\\n" for line in [*done, json.dumps(sys.argv[1:])]))
sys.stdout.write("\\x1b[?1000h")  # tmux mouse mode left on by the dropped link
sys.exit({list(codes)!r}[len(done)])
""")
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "remote-agent.py"), "attach", "job"],
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        output = b""
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
        os.close(master)
        code = process.wait(timeout=10)
        argvs = (
            [json.loads(line) for line in calls.read_text().splitlines()]
            if calls.exists()
            else []
        )
        return code, output, argvs

    def test_attach_reconnects_after_dropped_connections_and_resets_terminal(self):
        code, output, argvs = self.attach_with_mosh_exits(255, 1, 0)
        self.assertEqual(code, 0)
        self.assertEqual(len(argvs), 3)
        for argv in argvs:
            self.assertEqual(argv[0:3], ["--no-init", "--", "agent-server"])
            self.assertNotIn("desktop", argv)
            self.assertNotIn("-t", argv)
            self.assertNotIn("-o", argv)
            self.assertEqual(argv[-2:], ["--attach", "job"])
        self.assertEqual(output.count(b"reconnecting"), 2)
        # Every Mosh exit is followed by mouse tracking being switched off.
        self.assertEqual(output.count(b"\x1b[?1000h"), 3)
        self.assertTrue(output.rstrip().endswith(b"\x1b[?1049l\x1b[23;2t"))
        for after in output.split(b"\x1b[?1000h")[1:]:
            self.assertIn(b"\x1b[?1000l", after)

    def test_attach_stops_when_worker_refuses_session(self):
        code, output, argvs = self.attach_with_mosh_exits(0, session_exists=False)
        self.assertEqual(code, 1)
        self.assertEqual(len(argvs), 1)
        self.assertIn(b"no such session", output)
        self.assertNotIn(b"reconnecting", output)

    def test_status_logs_cancel_and_missing_prompt(self):
        for action in ("status", "logs", "cancel"):
            result = self.invoke(action, "custom")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)["request"],
                {"action": action, "name": "custom"},
            )
        self.assertNotEqual(
            self.invoke(
                "start",
                "engine",
                "job",
                "--prompt-file",
                str(self.root / "missing"),
            ).returncode,
            0,
        )


class SyncTests(unittest.TestCase):
    """Real git and rsync; fake ssh runs the WSL-side command locally."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.local = root / "laptop/game"
        self.remote = root / "desktop/Projects/game"
        self.remote.mkdir(parents=True)
        self.git(self.remote, "init", "-q", "-b", "master")
        (self.remote / ".gitignore").write_text("target/\n")
        (self.remote / "README").write_text("v1")
        self.git(self.remote, "add", ".")
        self.git(self.remote, "commit", "-q", "-m", "base")
        self.git(root, "clone", "-q", str(self.remote), str(self.local))
        self.native = root / "native/Projects/game"
        self.git(root, "clone", "-q", str(self.remote), str(self.native))
        self.ssh_calls = root / "ssh-calls"
        shutil.copy(ROOT / "remote-agent.py", root)
        self.controller = root / "remote-agent.py"
        tools = root / "tools"
        tools.mkdir()
        ssh = tools / "ssh"
        ssh.write_text(f"""#!{sys.executable}
import json, os, shlex, sys
args = sys.argv[1:]
while args[:1] == ['-o']:
    args = args[2:]
assert args[0] in ('desktop', 'agent-server'), sys.argv
with open({str(root / "ssh-calls")!r}, 'a') as calls:
    calls.write(json.dumps(args) + '\\n')
failures = {str(root / "ssh-failures")!r}
if os.path.exists(failures):
    left = int(open(failures).read())
    if left:
        open(failures, "w").write(str(left - 1))
        sys.exit(255)
argv = shlex.split(" ".join(args[1:]))
remote_home = {str(root)!r} + ('/desktop' if args[0] == 'desktop' else '/native')
os.chdir(remote_home)
if args[0] == 'desktop':
    if argv[5:7] == ["--cd", "~"]:
        del argv[5:7]
    assert argv[:6] == ["wsl.exe", "-d", "FixtureBuild", "-u", "fixture-test", "--exec"], argv
    argv = argv[6:]
else:
    assert 'wsl.exe' not in argv, argv
argv = [a.replace('/home/fixture-test', remote_home).replace('/home/fixture', remote_home) for a in argv]
os.execvp(argv[0], argv)
""")
        ssh.chmod(0o700)
        self.env = dict(os.environ, PATH=f"{tools}{os.pathsep}{os.environ['PATH']}")
        self.laptop_home = root / "laptop-home"
        self.laptop_home.mkdir()
        self.config = write_config(
            self.laptop_home,
            {"game": {"remote": "Projects/game", "local": str(self.local)}},
        )
        patch = mock.patch.dict(os.environ, {"HOME": str(self.laptop_home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.env["HOME"] = str(self.laptop_home)
        self.downloads = self.laptop_home / "Downloads/remote-agent"
        chromium = tools / "chromium"
        chromium.write_text(f"""#!{sys.executable}
import json, sys
with open({str(self.laptop_home / "chromium.log")!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
""")
        chromium.chmod(0o700)

    def git(self, repo, *args):
        return subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, repo, name, content):
        (repo / name).write_text(content)
        self.git(repo, "add", name)
        self.git(repo, "commit", "-q", "-m", name)
        return self.git(repo, "rev-parse", "HEAD")

    def sync(self, *args):
        return subprocess.run(
            [sys.executable, str(self.controller), "sync", *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def opened(self, launches):
        """Chromium is started detached; wait for the fake to record it."""
        log = self.laptop_home / "chromium.log"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lines = log.read_text().splitlines() if log.exists() else []
            if len(lines) >= launches:
                return [json.loads(line) for line in lines]
            time.sleep(0.05)
        self.fail("chromium was not launched")

    def fetch(self, *args):
        return subprocess.run(
            [sys.executable, str(self.controller), "fetch", *args],
            env={**self.env, "HOME": str(self.laptop_home)},
            capture_output=True,
            text=True,
        )

    def use_native_host(self):
        self.remote = self.native
        mapping = self.config / "remote-projects.json"
        projects = json.loads(mapping.read_text())
        projects["game"]["host"] = "desktop"
        mapping.write_text(json.dumps(projects))

    def test_native_sync_push_pull_and_fetch_spaces(self):
        self.use_native_host()
        head = self.commit(self.local, "README", "native push")
        (self.local / "work in progress.txt").write_text("dirty")
        result = self.sync("game")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "HEAD"), head)
        self.assertEqual((self.remote / "work in progress.txt").read_text(), "dirty")
        (self.remote / "work in progress.txt").unlink()
        self.commit(self.remote, "README", "native pull")
        (self.remote / "native shot.png").write_bytes(b"png")
        result = self.sync("game", "--pull", "--host", "agent-server")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.local / "README").read_text(), "native pull")
        result = self.fetch("native shot.png", "--project", "game")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "native shot.png").read_bytes(), b"png")
        result = self.fetch("native shot.png")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.ssh_calls.read_text().splitlines()]
        self.assertEqual({call[0] for call in calls}, {"agent-server"})
        self.assertEqual(
            (self.controller.parent / "desktop/Projects/game/README").read_text(), "v1"
        )

    def test_native_fetch_discovery_ignores_other_host_and_mapping_hosts(self):
        self.use_native_host()
        server = self.add_project("server", "Projects/server")
        (server / "shared.png").write_bytes(b"native")
        desktop = self.controller.parent / "desktop/Projects/game"
        (desktop / "shared.png").write_bytes(b"desktop")
        mapping = self.config / "remote-projects.json"
        projects = json.loads(mapping.read_text())
        projects["server"]["host"] = "obsolete-host"
        mapping.write_text(json.dumps(projects))
        result = self.fetch("shared.png")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "shared.png").read_bytes(), b"native")
        calls = [json.loads(line) for line in self.ssh_calls.read_text().splitlines()]
        self.assertEqual({call[0] for call in calls}, {"agent-server"})
        self.ssh_calls.unlink()
        result = self.fetch("shared.png", "--host", "desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "shared.png").read_bytes(), b"desktop")
        calls = [json.loads(line) for line in self.ssh_calls.read_text().splitlines()]
        self.assertEqual({call[0] for call in calls}, {"desktop"})

    def test_native_fetch_absolute_and_home_paths_default_to_native(self):
        (self.native / "native.png").write_bytes(b"native")
        for path in (
            "/home/fixture/Projects/game/native.png",
            "~/Projects/game/native.png",
        ):
            result = self.fetch(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.downloads / "native.png").read_bytes(), b"native")
        calls = [json.loads(line) for line in self.ssh_calls.read_text().splitlines()]
        self.assertEqual({call[0] for call in calls}, {"agent-server"})

    def test_native_fetch_unknown_project_fails_before_ssh(self):
        result = self.fetch("/home/fixture/x.png", "--project", "nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project must be one of: game", result.stderr)
        self.assertFalse(self.ssh_calls.exists())

    def test_native_fetch_rejects_windows_unc_path(self):
        self.use_native_host()
        for options in ((), ("--project", "game"), ("--host", "agent-server")):
            result = self.fetch(
                "\\\\wsl$\\FixtureBuild\\home\\fixture-test\\Projects\\game\\a.png",
                *options,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("UNC paths require desktop", result.stderr)
        self.assertFalse(self.ssh_calls.exists())

    def test_fetch_pasted_windows_paths_into_downloads(self):
        (self.remote / "shots").mkdir()
        (self.remote / "shots/login screen.png").write_bytes(b"\x89PNG fake")
        (self.remote / "shots/b.webp").write_bytes(b"webp")
        unc = "\\\\wsl$\\FixtureBuild\\home\\fixture-test\\Projects\\game\\shots\\"
        result = self.fetch(
            "--host", "desktop", unc + "login screen.png", unc + "b.webp"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.downloads / "login screen.png").read_bytes(), b"\x89PNG fake"
        )
        self.assertEqual((self.downloads / "b.webp").read_bytes(), b"webp")
        self.assertIn(str(self.downloads / "b.webp"), result.stdout)

    def test_fetch_folder_mirrors_incrementally_and_opens_only_changed_images(self):
        (self.remote / "shots").mkdir()
        (self.remote / "shots/a.png").write_bytes(b"a")
        (self.remote / "shots/run.log").write_text("log")
        first = self.fetch(
            "--host", "desktop", "/home/fixture-test/Projects/game/shots", "--open"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.opened(1), [[f"file://{self.downloads}/shots/a.png"]])
        (self.remote / "shots/b.png").write_bytes(b"b")
        second = self.fetch("--host", "desktop", "shots", "--project", "game", "--open")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.downloads / "shots/run.log").read_text(), "log")
        self.assertEqual(self.opened(2)[-1], [f"file://{self.downloads}/shots/b.png"])

    def test_fetch_open_unchanged_folder_shows_all_its_images(self):
        (self.remote / "shots").mkdir()
        (self.remote / "shots/a.png").write_bytes(b"a")
        (self.remote / "shots/b.webp").write_bytes(b"b")
        (self.remote / "shots/run.log").write_text("log")
        self.assertEqual(
            self.fetch("--host", "desktop", "shots", "--project", "game").returncode, 0
        )
        result = self.fetch("--host", "desktop", "shots", "--project", "game", "--open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sorted(self.opened(1)[0]),
            [
                f"file://{self.downloads}/shots/a.png",
                f"file://{self.downloads}/shots/b.webp",
            ],
        )

    def test_fetch_outside_projects_goes_to_downloads(self):
        notes = self.remote.parent.parent / "notes"
        notes.mkdir()
        (notes / "x.png").write_bytes(b"x")
        result = self.fetch("--host", "desktop", "~/notes/x.png")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "x.png").read_bytes(), b"x")

    def add_project(self, name, remote):
        projects_file = self.config / "remote-projects.json"
        projects = json.loads(projects_file.read_text())
        projects[name] = {"remote": remote, "local": str(self.local)}
        projects_file.write_text(json.dumps(projects))
        path = self.remote.parent.parent / remote
        path.mkdir(parents=True)
        return path

    def test_fetch_relative_path_without_project_uses_the_project_that_has_it(self):
        server = self.add_project("server", "Projects/server")
        (self.remote / "data/diag dir").mkdir(parents=True)
        (self.remote / "data/diag dir/pip 4x.png").write_bytes(b"pip")
        (server / "data").mkdir()
        result = self.fetch("--host", "desktop", "data/diag dir/pip 4x.png", "--open")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "pip 4x.png").read_bytes(), b"pip")
        self.assertEqual(self.opened(1), [[f"file://{self.downloads}/pip 4x.png"]])

    def test_fetch_relative_path_in_several_or_no_projects_needs_project(self):
        server = self.add_project("server", "Projects/server")
        (self.remote / "shared.png").write_bytes(b"game")
        (server / "shared.png").write_bytes(b"server")
        result = self.fetch("--host", "desktop", "shared.png")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "shared.png exists in game, server; pass --project", result.stderr
        )
        result = self.fetch("--host", "desktop", "shared.png", "--project", "server")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "shared.png").read_bytes(), b"server")
        result = self.fetch("--host", "desktop", "missing.png")
        self.assertIn("missing.png exists in no project; pass --project", result.stderr)
        self.assertFalse((self.downloads / "missing.png").exists())

    def test_fetch_retries_dropped_desktop_connections(self):
        (self.remote / "data").mkdir()
        (self.remote / "data/pip.png").write_bytes(b"pip")
        failures = self.laptop_home.parent / "ssh-failures"
        failures.write_text("2")
        result = self.fetch("--host", "desktop", "data/pip.png")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.downloads / "pip.png").read_bytes(), b"pip")
        self.assertEqual(result.stderr.count("desktop connection failed"), 2)
        self.assertEqual(failures.read_text(), "0")

    def test_fetch_missing_file_in_named_project_fails(self):
        result = self.fetch("--host", "desktop", "missing.png", "--project", "game")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.downloads / "missing.png").exists())

    def test_push_fast_forwards_commits_then_copies_uncommitted_files(self):
        head = self.commit(self.local, "README", "v2")
        (self.local / "wip.rs").write_text("uncommitted")
        (self.local / "target").mkdir()
        (self.local / "target/big.bin").write_text("artifact")
        (self.local / ".git/info/exclude").write_text("*.pyc\n")
        (self.local / "cache.pyc").write_text("excluded outside .gitignore")
        (self.remote / "remote-only.txt").write_text("keep")

        result = self.sync("--host", "desktop", "game", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("wip.rs", result.stdout)
        self.assertNotEqual(self.git(self.remote, "rev-parse", "HEAD"), head)
        self.assertFalse((self.remote / "wip.rs").exists())

        result = self.sync("--host", "desktop", "game")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.remote, "rev-parse", "HEAD"), head)
        self.assertEqual((self.remote / "README").read_text(), "v2")
        self.assertEqual((self.remote / "wip.rs").read_text(), "uncommitted")
        self.assertFalse((self.remote / "target").exists())
        self.assertFalse((self.remote / "cache.pyc").exists())
        self.assertEqual((self.remote / "remote-only.txt").read_text(), "keep")
        self.assertIn("wip.rs", self.git(self.remote, "status", "--short"))

    def test_push_refuses_dirty_desktop_tree_and_copies_nothing(self):
        self.commit(self.local, "README", "v2")
        (self.local / "wip.rs").write_text("uncommitted")
        (self.remote / "README").write_text("agent edit")

        result = self.sync("--host", "desktop", "game")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.remote / "README").read_text(), "agent edit")
        self.assertFalse((self.remote / "wip.rs").exists())

    def test_push_refuses_diverged_desktop_commits(self):
        self.commit(self.local, "laptop.txt", "l")
        desktop_head = self.commit(self.remote, "desktop.txt", "d")
        result = self.sync("--host", "desktop", "game")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.remote, "rev-parse", "HEAD"), desktop_head)

    def test_pull_fast_forwards_desktop_commits_and_copies_files(self):
        head = self.commit(self.remote, "agent.txt", "overnight work")
        (self.remote / "notes.md").write_text("uncommitted")
        (self.remote / "README").unlink()
        result = self.sync("--host", "desktop", "game", "--pull")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), head)
        self.assertEqual((self.local / "notes.md").read_text(), "uncommitted")
        self.assertEqual((self.local / "README").read_text(), "v1")

    def test_clean_tree_reports_nothing_to_copy(self):
        result = self.sync("--host", "desktop", "game")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No uncommitted files", result.stdout)

    def test_file_list_accepts_only_known_windows_trailer(self):
        spec = importlib.util.spec_from_file_location("controller", self.controller)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            module.parse_file_list(b"a\0b c\0\x1b]2;Terminal\x07"), b"a\0b c\0"
        )
        self.assertEqual(module.parse_file_list(b""), b"")
        with self.assertRaises(ValueError):
            module.parse_file_list(b"a\0garbage")

    def test_unknown_project_fails_with_choices(self):
        result = self.sync("nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("game", result.stderr)


if __name__ == "__main__":
    unittest.main()

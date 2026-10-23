"""Local executable-boundary tests; no Docker daemon or remote host required."""

import hashlib
import importlib.util
import json
import os
import pty
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "sandbox" / "session.py"

FIXTURE = r"""#!/usr/bin/env python3
import json, os, pathlib, signal, sys, time
state_path = pathlib.Path(os.environ['FIXTURE_STATE'])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
state['commands'].append([tool, *args])
def save():
    temporary = state_path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(state))
    os.replace(temporary, state_path)
def finish(code=0, message=''):
    save()
    if message: print(message, file=sys.stderr)
    sys.exit(code)
def pause(stage):
    global state
    if not state.get('block_attach'): return
    save()
    ready = state_path.with_suffix('.'+stage)
    ready.touch()
    deadline = time.monotonic() + 8
    while not state_path.with_suffix('.release-'+stage).exists():
        if time.monotonic() >= deadline: sys.exit(98)
        time.sleep(0.01)
    state = json.loads(state_path.read_text())
if tool == 'systemctl':
    finish(0 if state.get('firewall', True) else 3, '' if state.get('firewall', True) else 'inactive')
if state.get('fail') == args[0]: finish(42, 'fixture Docker failure')
if args[0] == 'inspect':
    name = args[-1]
    if name not in state['containers']: finish(1, 'Error: No such object: '+name)
    c = state['containers'][name]
    print(json.dumps([{'Config': {'Labels': {'remote-agent.conversation': c['label']}}, 'State': {'Status': c['status']}}]))
    finish()
if args[0] == 'rm':
    del state['containers'][args[-1]]
    finish()
if args[0] in ('create', 'run'):
    # All sandbox options that take a value; stop at the image.
    valued = {'--user','--cap-drop','--security-opt','--pids-limit','--memory','--cpus', '--network','--dns','--sysctl','--tmpfs','--mount','--name','--label','--workdir','--env'}
    i = 1
    while i < len(args) and args[i].startswith('--'):
        i += 2 if args[i] in valued else 1
    image, forwarded = args[i], args[i+1:]
    if args[0] == 'run':
        state['transient'] = {'image': image, 'args': forwarded}
        print(json.dumps(forwarded))
        pause('running')
        finish(state.get('exit_code', 0))
    name = args[args.index('--name')+1]
    if name in state['containers']: finish(1, 'name conflict')
    label = args[args.index('--label')+1].split('=', 1)[1]
    state['containers'][name] = {'label': label, 'status': 'created', 'args': forwarded, 'files': {}}
    if state.get('disable_firewall_after_create'): state['firewall'] = False
    print(name)
    finish()
if args[0] == 'cp':
    src, dest = args[-2:]
    name, target = dest.split(':', 1)
    p = pathlib.Path(src)
    state['containers'][name]['files'][target] = {'text': p.read_text(), 'mode': p.stat().st_mode & 0o777}
    finish()
if args[0] == 'kill':
    if state['containers'][args[-1]]['status'] != 'running':
        finish(1, 'container is not running')
    finish()
if args[0] == 'start':
    pause('created')
    c = state['containers'][args[-1]]
    c['status'] = 'running' if state.get('keep_running') else 'exited'
    print(json.dumps({'args': c['args'], 'files': c['files']}))
    pause('running')
    finish(state.get('exit_code', 0))
finish(2, 'unexpected fixture command: '+repr(args))
"""


def container_name(conversation):
    return "clank-pi-" + hashlib.sha256(conversation.encode()).hexdigest()[:32]


class SandboxSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state_file = self.base / "state.json"
        self.save({"containers": {}, "commands": []})
        for tool in ("docker", "systemctl"):
            executable = self.base / tool
            executable.write_text(FIXTURE)
            executable.chmod(0o755)
        self.secret = self.base / "openrouter.key"
        self.secret.write_text("test-key-never-in-argv")
        self.config = self.base / "runtime.json"
        self.settings = {
            "image": "clank-pi@sha256:abc123",
            "network": "clank-sandbox",
            "home_volume": "clank-pi-home",
            "workspace_volume": "clank-pi-workspace",
            "secret_file": str(self.secret),
            "firewall_service": "clank-sandbox-firewall.service",
        }
        self.config.write_text(json.dumps(self.settings))
        self.env = {
            **os.environ,
            "PATH": str(self.base) + os.pathsep + os.environ["PATH"],
            "FIXTURE_STATE": str(self.state_file),
            "OPENROUTER_API_KEY": "do-not-forward",
            "SSH_AUTH_SOCK": "/private/agent.sock",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
        }

    def save(self, state):
        temporary = self.state_file.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(state))
        os.replace(temporary, self.state_file)

    def state(self):
        return json.loads(self.state_file.read_text())

    def invoke(self, *args, tty=False):
        command = [sys.executable, str(WRAPPER), "--config", str(self.config), *args]
        if not tty:
            return subprocess.run(
                command, env=self.env, text=True, capture_output=True, check=False
            )
        master, slave = pty.openpty()
        try:
            return subprocess.run(
                command,
                env=self.env,
                stdin=slave,
                stdout=slave,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        finally:
            os.close(slave)
            os.close(master)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("fixture did not reach expected state")

    def start_blocked(self, *arguments, **settings):
        self.save({**self.state(), "block_attach": True, **settings})
        process = subprocess.Popen(
            [sys.executable, str(WRAPPER), "--config", str(self.config), *arguments],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def cleanup():
            for stage in ("created", "running"):
                self.state_file.with_suffix(".release-" + stage).touch()
            try:
                process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.addCleanup(cleanup)
        return process

    def release(self, stage):
        self.state_file.with_suffix(".release-" + stage).touch()

    def kill_commands(self):
        return [c for c in self.state()["commands"] if c[:2] == ["docker", "kill"]]

    def test_winch_forwards_only_running_named_container_while_attach_waits(self):
        foreign = container_name("foreign")
        self.save(
            {
                "containers": {foreign: {"label": "foreign", "status": "running"}},
                "commands": [],
            }
        )
        process = self.start_blocked("--session", "selected", keep_running=True)
        self.wait_for(lambda: self.state_file.with_suffix(".created").exists())
        before = len(self.state()["commands"])
        os.kill(process.pid, signal.SIGWINCH)
        self.wait_for(lambda: len(self.state()["commands"]) > before)
        self.assertEqual(self.kill_commands(), [])
        self.release("created")
        self.wait_for(lambda: self.state_file.with_suffix(".running").exists())
        os.kill(process.pid, signal.SIGWINCH)
        self.wait_for(lambda: bool(self.kill_commands()))
        self.assertIsNone(process.poll())
        self.assertEqual(
            self.kill_commands(),
            [["docker", "kill", "--signal", "WINCH", container_name("selected")]],
        )
        self.assertEqual(self.state()["containers"][foreign]["status"], "running")
        self.release("running")
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)

    def test_repeated_winch_publications_remain_complete_while_polled(self):
        payload = "state remains complete" * 4096
        process = self.start_blocked(
            "--session", "selected", keep_running=True, payload=payload
        )
        self.wait_for(lambda: self.state_file.with_suffix(".created").exists())
        self.release("created")
        self.wait_for(lambda: self.state_file.with_suffix(".running").exists())
        expected = ["docker", "kill", "--signal", "WINCH", container_name("selected")]
        for count in range(1, 33):
            with self.subTest(signal=count):
                os.kill(process.pid, signal.SIGWINCH)
                self.wait_for(lambda count=count: len(self.kill_commands()) == count)
                self.assertEqual(self.kill_commands(), [expected] * count)
                self.assertEqual(self.state()["payload"], payload)
                self.assertIsNone(process.poll())
        self.release("running")
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(self.kill_commands(), [expected] * 32)
        self.assertEqual(self.state()["payload"], payload)

    def test_winch_docker_failure_is_reported_without_ending_attach(self):
        process = self.start_blocked(
            "--session", "selected", keep_running=True, fail="kill"
        )
        self.wait_for(lambda: self.state_file.with_suffix(".created").exists())
        self.release("created")
        self.wait_for(lambda: self.state_file.with_suffix(".running").exists())
        os.kill(process.pid, signal.SIGWINCH)
        self.wait_for(lambda: bool(self.kill_commands()))
        self.assertIsNone(process.poll())
        self.release("running")
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("fixture Docker failure", stderr)
        self.assertIn(container_name("selected"), stderr)

    def test_winch_transient_has_no_container_signal_target(self):
        process = self.start_blocked("--help")
        self.wait_for(lambda: self.state_file.with_suffix(".running").exists())
        os.kill(process.pid, signal.SIGWINCH)
        self.release("running")
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(self.kill_commands(), [])

    def test_run_session_restores_previous_winch_handler_on_exit(self):
        spec = importlib.util.spec_from_file_location("sandbox_session", WRAPPER)
        session = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(session)
        observed = []
        previous = signal.signal(
            signal.SIGWINCH, lambda signum, frame: observed.append(signum)
        )
        self.addCleanup(signal.signal, signal.SIGWINCH, previous)
        with patch.dict(os.environ, self.env):
            for fail in (None, "start"):
                with self.subTest(fail=fail):
                    self.save({"containers": {}, "commands": [], "fail": fail})
                    expected = signal.getsignal(signal.SIGWINCH)
                    self.assertEqual(
                        session.run_session(
                            self.settings, ["--session", "direct"], "direct"
                        ),
                        42 if fail else 0,
                    )
                    self.assertIs(signal.getsignal(signal.SIGWINCH), expected)
                    os.kill(os.getpid(), signal.SIGWINCH)
        self.assertEqual(observed, [signal.SIGWINCH, signal.SIGWINCH])

    def test_two_conversations_cancel_only_selected_and_repeat_is_idempotent(self):
        state = self.state()
        state["keep_running"] = True
        self.save(state)
        for conversation in ("first/id", "second"):
            self.assert_success(self.invoke("--session", conversation, "hello"))
        first, second = map(container_name, ("first/id", "second"))
        self.assertEqual(set(self.state()["containers"]), {first, second})
        self.assert_success(self.invoke("--cancel", "first/id"))
        self.assertEqual(set(self.state()["containers"]), {second})
        self.assertEqual(self.state()["containers"][second]["status"], "running")
        self.assert_success(self.invoke("--cancel", "first/id"))
        removals = [c for c in self.state()["commands"] if c[:2] == ["docker", "rm"]]
        self.assertEqual(removals, [["docker", "rm", "--force", "--volumes", first]])

    def test_cancel_exited_container_removes_anonymous_volumes(self):
        name = container_name("finished")
        self.save(
            {
                "containers": {name: {"label": "finished", "status": "exited"}},
                "commands": [],
            }
        )
        self.assert_success(self.invoke("--cancel", "finished"))
        self.assertEqual(self.state()["containers"], {})
        self.assertEqual(
            self.state()["commands"][-1],
            ["docker", "rm", "--force", "--volumes", name],
        )

    def test_session_id_creates_named_labeled_container_on_configured_network(self):
        conversation = "d50a3efb-4be5-41b5-a88b-70295b1c7b57"
        args = ["--session-id", conversation, "hello"]
        self.assert_success(self.invoke(*args))
        state = self.state()
        name = container_name(conversation)
        self.assertEqual(set(state["containers"]), {name})
        self.assertEqual(state["containers"][name]["label"], conversation)
        self.assertEqual(state["containers"][name]["args"], args)
        create = next(c for c in state["commands"] if c[:2] == ["docker", "create"])
        self.assertEqual(create[create.index("--name") + 1], name)
        self.assertEqual(
            create[create.index("--network") + 1], self.settings["network"]
        )
        self.assertEqual(
            state["commands"][-1],
            ["docker", "start", "--attach", "--interactive", name],
        )
        self.assertNotIn("transient", state)

    def test_foreign_label_refuses_launch_and_cancel(self):
        name = container_name("one")
        original = {"label": "someone-else", "status": "exited"}
        self.save({"containers": {name: original}, "commands": []})
        for args in (("--session", "one"), ("--cancel", "one")):
            result = self.invoke(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("label", result.stderr)
        self.assertEqual(self.state()["containers"][name], original)

    def test_running_refused_exited_recreated_for_resume(self):
        state = self.state()
        state["keep_running"] = True
        self.save(state)
        self.assert_success(self.invoke("--session", "one"))
        result = self.invoke("--resume", "one")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("running", result.stderr)
        state = self.state()
        state["containers"][container_name("one")]["status"] = "exited"
        self.save(state)
        self.assert_success(self.invoke("--resume", "one", "next"))
        commands = self.state()["commands"]
        removals = [c for c in commands if c[:2] == ["docker", "rm"]]
        self.assertEqual(
            removals, [["docker", "rm", "--volumes", container_name("one")]]
        )
        replacement = next(
            i
            for i, command in enumerate(commands)
            if command[:2] == ["docker", "create"] and "next" in command
        )
        self.assertLess(commands.index(removals[0]), replacement)
        self.assertEqual(
            self.state()["containers"][container_name("one")]["args"],
            ["--resume", "one", "next"],
        )

    def test_restrictions_exact_argv_and_no_host_environment_or_root_mount(self):
        args = [
            "--session",
            "a b",
            "--model",
            "provider/model",
            "--",
            "hello 'world'",
            "$(touch /bad)",
        ]
        self.assert_success(self.invoke(*args))
        state = self.state()
        create = next(c[2:] for c in state["commands"] if c[:2] == ["docker", "create"])
        self.assertEqual(create[-len(args) :], args)
        for flag in ("--init", "--read-only", "--interactive"):
            self.assertIn(flag, create)
        self.assertNotIn("--tty", create)
        self.assertNotIn("--tmpfs", create)
        expected = {
            "--user": "1000:1000",
            "--cap-drop": "ALL",
            "--security-opt": "no-new-privileges",
            "--pids-limit": "256",
            "--memory": "4g",
            "--cpus": "4",
            "--network": "clank-sandbox",
            "--sysctl": "net.ipv6.conf.all.disable_ipv6=1",
            "--workdir": "/workspace",
        }
        for flag, value in expected.items():
            self.assertEqual(create[create.index(flag) + 1], value)
        self.assertEqual(
            [create[i + 1] for i, x in enumerate(create) if x == "--dns"],
            ["1.1.1.1", "8.8.8.8"],
        )
        mounts = [create[i + 1] for i, x in enumerate(create) if x == "--mount"]
        self.assertEqual(
            set(mounts),
            {
                "type=volume,dst=/tmp",
                "type=volume,src=clank-pi-home,dst=/home/agent",
                "type=volume,src=clank-pi-workspace,dst=/workspace",
                f"type=bind,src={self.secret},dst=/run/secrets/openrouter.key,readonly",
            },
        )
        for forbidden in (
            "--env-file",
            "--volume",
            "-v",
            "do-not-forward",
            "/private/agent.sock",
            "test-key-never-in-argv",
        ):
            self.assertNotIn(forbidden, create)
        self.assertEqual(
            state["commands"][-1],
            ["docker", "start", "--attach", "--interactive", container_name("a b")],
        )

    def test_only_safe_terminal_environment_forwarded(self):
        self.assert_success(self.invoke("--session", "terminal"))
        create = next(
            c for c in self.state()["commands"] if c[:2] == ["docker", "create"]
        )
        environment = [create[i + 1] for i, arg in enumerate(create) if arg == "--env"]
        self.assertEqual(environment, ["TERM=xterm-256color", "COLORTERM=truecolor"])
        for key, value in (("TERM", "xterm\n--privileged"), ("COLORTERM", "not-color")):
            with self.subTest(key=key):
                original = self.env[key]
                self.env[key] = value
                result = self.invoke("--session", "bad-terminal")
                self.env[key] = original
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)

    def test_firewall_rechecked_after_create_before_start(self):
        state = self.state()
        state["disable_firewall_after_create"] = True
        self.save(state)
        result = self.invoke("--session", "one")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("clank-sandbox-firewall.service", result.stderr)
        self.assertEqual(
            self.state()["containers"][container_name("one")]["status"], "created"
        )
        self.assertFalse(
            any(c[:2] == ["docker", "start"] for c in self.state()["commands"])
        )

    def test_default_config_is_installed_sibling(self):
        installed = self.base / "session.py"
        installed.write_bytes(WRAPPER.read_bytes())
        result = subprocess.run(
            [sys.executable, str(installed), "--session", "one"],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assert_success(result)
        self.assertIn(container_name("one"), self.state()["containers"])

    def test_transient_prompt_rejection_is_explicit(self):
        prompt = self.base / "prompt.txt"
        prompt.write_text("hello")
        result = self.invoke("@" + str(prompt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("require --session", result.stderr)
        self.assertEqual(self.state()["containers"], {})

    def test_tty_only_when_callers_terminal(self):
        self.assert_success(self.invoke("--session", "terminal", tty=True))
        create = next(
            c for c in self.state()["commands"] if c[:2] == ["docker", "create"]
        )
        self.assertIn("--tty", create)

    def test_firewall_inactive_prevents_creation(self):
        state = self.state()
        state["firewall"] = False
        self.save(state)
        for args in (("--session", "one"), ("--help",)):
            result = self.invoke(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("firewall", result.stderr.lower())
        self.assertEqual(self.state()["containers"], {})
        self.assertFalse(
            any(c[1] in ("create", "start", "run") for c in self.state()["commands"])
        )

    def test_transient_network_none_unnamed_auto_remove(self):
        args = ["--export", "/home/agent/session.json", "--output", "-"]
        self.assert_success(self.invoke(*args))
        state = self.state()
        command = next(c for c in state["commands"] if c[:2] == ["docker", "run"])
        self.assertIn("--rm", command)
        self.assertNotIn("--tmpfs", command)
        mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
        self.assertEqual(
            [mount for mount in mounts if "dst=/tmp" in mount],
            ["type=volume,dst=/tmp"],
        )
        self.assertNotIn("--name", command)
        self.assertNotIn("--label", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(state["transient"]["args"], args)
        self.assertEqual(state["containers"], {})

    def test_prompt_copy_private_volume_random_readable_files_and_original_unchanged(
        self,
    ):
        prompt = self.base / "prompt.txt"
        prompt.write_text("exact prompt\nsecond line\n")
        prompt.chmod(0o600)
        for conversation in ("one", "two"):
            self.assert_success(
                self.invoke("--session", conversation, "@" + str(prompt))
            )
        targets = []
        for container in self.state()["containers"].values():
            target = container["args"][-1][1:]
            self.assertTrue(target.startswith("/home/agent/.remote-agent-prompts/"))
            targets.append(target)
            self.assertEqual(
                container["files"][target], {"text": prompt.read_text(), "mode": 0o644}
            )
        self.assertNotEqual(*targets)
        self.assertEqual(prompt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prompt.read_text(), "exact prompt\nsecond line\n")

    def test_prompt_rejects_relative_missing_and_oversize_before_creation(self):
        big = self.base / "big.txt"
        big.write_bytes(b"x" * (1024 * 1024 + 1))
        for path in ("relative.txt", str(self.base / "absent"), str(big)):
            result = self.invoke("--session", "one", "@" + path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("prompt", result.stderr.lower())
        self.assertEqual(self.state()["containers"], {})

    def test_docker_failures_and_child_exit_status_explicit(self):
        for operation in ("inspect", "create", "cp", "start", "rm", "run"):
            with self.subTest(operation=operation):
                state = {"containers": {}, "commands": [], "fail": operation}
                if operation == "rm":
                    state["containers"][container_name("one")] = {
                        "label": "one",
                        "status": "exited",
                    }
                self.save(state)
                prompt = self.base / "prompt.txt"
                prompt.write_text("hello")
                args = (
                    ("--help",)
                    if operation == "run"
                    else ("--session", "one", "@" + str(prompt))
                )
                result = self.invoke(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("fixture Docker failure", result.stderr)
        self.save({"containers": {}, "commands": [], "exit_code": 17})
        self.assertEqual(self.invoke("--session", "one").returncode, 17)

    def test_non_object_config_raises_type_error_and_cli_reports_it(self):
        spec = importlib.util.spec_from_file_location("sandbox_session", WRAPPER)
        session = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(session)
        for config in (None, [], "invalid", 123):
            with self.subTest(config=config):
                self.config.write_text(json.dumps(config))
                with self.assertRaisesRegex(
                    TypeError, "runtime config must be a JSON object"
                ):
                    session.load_config(self.config)
                result = self.invoke("--session", "one")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr,
                    "sandbox session: runtime config must be a JSON object\n",
                )
                self.assertEqual(self.state()["commands"], [])

    def test_invalid_config_and_ambiguous_identity_fail_explicitly(self):
        for key, bad in (
            ("secret_file", "relative"),
            ("home_volume", "/"),
            ("image", ""),
        ):
            config = {**self.settings, key: bad}
            self.config.write_text(json.dumps(config))
            result = self.invoke("--session", "one")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(key, result.stderr)
        self.config.write_text(json.dumps(self.settings))
        for args in (
            ("--session",),
            ("--resume",),
            ("--session", "one", "--resume", "two"),
            ("--cancel",),
            ("--cancel", "one", "extra"),
        ):
            self.assertNotEqual(self.invoke(*args).returncode, 0)


if __name__ == "__main__":
    unittest.main()

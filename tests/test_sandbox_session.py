"""Local executable-boundary tests; no Docker daemon or remote host required."""

import hashlib
import json
import os
import pty
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "sandbox" / "session.py"

FIXTURE = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
state_path = pathlib.Path(os.environ['FIXTURE_STATE'])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
state['commands'].append([tool, *args])
def finish(code=0, message=''):
    state_path.write_text(json.dumps(state))
    if message: print(message, file=sys.stderr)
    sys.exit(code)
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
if args[0] == 'start':
    c = state['containers'][args[-1]]
    c['status'] = 'running' if state.get('keep_running') else 'exited'
    print(json.dumps({'args': c['args'], 'files': c['files']}))
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
        self.state_file.write_text(json.dumps(state))

    def state(self):
        return json.loads(self.state_file.read_text())

    def invoke(self, *args, tty=False):
        command = [sys.executable, str(WRAPPER), "--config", str(self.config), *args]
        if not tty:
            return subprocess.run(command, env=self.env, text=True, capture_output=True)
        master, slave = pty.openpty()
        try:
            return subprocess.run(
                command,
                env=self.env,
                stdin=slave,
                stdout=slave,
                stderr=subprocess.PIPE,
                text=True,
            )
        finally:
            os.close(slave)
            os.close(master)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)

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
        self.assertEqual(removals, [["docker", "rm", "--force", first]])

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
        expected = {
            "--user": "1000:1000",
            "--cap-drop": "ALL",
            "--security-opt": "no-new-privileges",
            "--pids-limit": "256",
            "--memory": "4g",
            "--cpus": "4",
            "--network": "clank-sandbox",
            "--sysctl": "net.ipv6.conf.all.disable_ipv6=1",
            "--tmpfs": "/tmp:rw,exec,nosuid,nodev,size=512m",
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

"""Unified login through the real Rust CLI and isolated executable fixtures.

SSH transport/account switching are substituted; the CLI, login dispatch, TTY,
stdin, exit handling and credential-file side effects are real subprocesses.
No network request, actual login or real credential file is used.
"""

import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RustLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(
            os.environ.get(
                "REMOTE_AGENT_RUST_BIN", ROOT / "target/debug/remote-agent-rs"
            )
        ).resolve()
        if not cls.binary.is_file():
            raise unittest.SkipTest(f"Rust executable missing: {cls.binary}")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="remote-agent-login-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.operator = self.root / "operator"
        self.target = self.root / "allison"
        for home in [self.operator, self.target]:
            config = home / ".config/remote-agent"
            config.mkdir(parents=True)
            (config / "remote-hosts.json").write_text("{}")
            (config / "remote-projects.json").write_text("{}")
            native = home / ".claude"
            native.mkdir()
            (native / ".credentials.json").write_text('{"existing":"native"}')
            pi = home / ".config/pi/agent"
            pi.mkdir(parents=True)
            (pi / "auth.json").write_text('{"existing":"pi"}')
            (home / ".local/bin").mkdir(parents=True)
        hosts = self.operator / ".config/remote-agent/remote-hosts.json"
        hosts.write_text(
            json.dumps(
                {"agent-server": {"ssh": "fixture-host", "home": str(self.operator)}}
            )
        )
        (self.operator / ".local/bin/remote-agent-rs").symlink_to(self.binary)
        self.install_executable("claude", "native")
        self.install_executable("pi", "pi")
        ssh = self.operator / ".local/bin/ssh"
        ssh.write_text(
            f"#!{sys.executable}\n"
            "import json,os,shlex,sys\n"
            "from pathlib import Path\n"
            "assert sys.argv[1:3] == ['-t','fixture-host'], sys.argv[1:]\n"
            "argv=shlex.split(sys.argv[3])\n"
            "home=Path(os.environ['FIXTURE_OPERATOR_HOME'])\n"
            "assert argv.pop(0)==str(home/'.local/bin/remote-agent-rs')\n"
            "if argv[:1]==['__as-user']:\n"
            " assert argv[1]=='allison'\n"
            " home=Path(os.environ['FIXTURE_TARGET_HOME']); argv=argv[2:]\n"
            "assert argv[:1]==['__login'] and len(argv) in (2,3), argv\n"
            "os.environ['HOME']=str(home)\n"
            "os.chdir(home)\n"
            f"os.execv({str(self.binary)!r},[{str(self.binary)!r},*argv])\n"
        )
        ssh.chmod(0o700)
        self.env = {
            **os.environ,
            "HOME": str(self.operator),
            "PATH": str(self.operator / ".local/bin") + ":" + os.environ["PATH"],
            "FIXTURE_OPERATOR_HOME": str(self.operator),
            "FIXTURE_TARGET_HOME": str(self.target),
            "ANTHROPIC_API_KEY": "fixture-unwanted-key",
            "ANTHROPIC_AUTH_TOKEN": "fixture-unwanted-token",
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-unwanted-oauth",
        }
        self.env.pop("CLAUDE_CONFIG_DIR", None)

    def install_executable(self, executable, store):
        for home in [self.operator, self.target]:
            path = home / ".local/bin" / executable
            path.write_text(
                f"#!{sys.executable}\n"
                "import json,os,sys\n"
                "from pathlib import Path\n"
                f"store={store!r}\n"
                "home=Path.home()\n"
                "if store=='native':\n"
                " assert sys.argv[1:]==['auth','login','--claudeai'], sys.argv[1:]\n"
                " assert not any(key in os.environ for key in "
                "['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN'])\n"
                " path=Path(os.environ.get('CLAUDE_CONFIG_DIR',home/'.claude'))/'.credentials.json'\n"
                "else:\n"
                " assert sys.argv[1:]==['login','openai-codex'], sys.argv[1:]\n"
                " path=home/'.config/pi/agent/auth.json'\n"
                "print('LOGIN_READY',flush=True)\n"
                "line=sys.stdin.readline().strip()\n"
                "if line!='confirm': sys.exit(23)\n"
                "path.parent.mkdir(parents=True,exist_ok=True)\n"
                "path.write_text(json.dumps({'authenticated':store,'home':str(home),"
                "'tty':sys.stdin.isatty() and sys.stdout.isatty()}))\n"
                "print('LOGIN_COMPLETE',flush=True)\n"
            )
            path.chmod(0o700)

    def run_login(
        self, provider="claude", account=None, *, user=True, confirmation="confirm\n"
    ):
        arguments = [str(self.binary), "login", provider]
        if account:
            arguments.append(account)
        if user:
            arguments.extend(["--user", "allison"])
        return subprocess.run(
            arguments,
            input=confirmation,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def read_store(self, home, native):
        path = home / (
            ".claude/.credentials.json" if native else ".config/pi/agent/auth.json"
        )
        return json.loads(path.read_text())

    def test_claude_login_updates_only_selected_account_native_store(self):
        result = self.run_login()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LOGIN_COMPLETE", result.stdout)
        grant = self.read_store(self.target, True)
        self.assertEqual(grant["authenticated"], "native")
        self.assertEqual(grant["home"], str(self.target))
        self.assertEqual(self.read_store(self.target, False), {"existing": "pi"})
        self.assertEqual(self.read_store(self.operator, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.operator, False), {"existing": "pi"})

    def test_other_provider_uses_pi_store_not_claude_store(self):
        result = self.run_login("openai-codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_store(self.target, False)["authenticated"], "pi")
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.operator, False), {"existing": "pi"})

    def test_default_account_login_does_not_select_another_unix_user(self):
        result = self.run_login(user=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.read_store(self.operator, True)["home"], str(self.operator)
        )
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})

    def test_explicit_native_config_directory_is_retained(self):
        config = self.target / "selected-native-config"
        self.env["CLAUDE_CONFIG_DIR"] = str(config)
        result = self.run_login()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((config / ".credentials.json").read_text())["authenticated"],
            "native",
        )
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})

    def profile(self, account):
        return self.target / ".config/pi-claude-bridge/profiles" / account

    def test_claude_account_login_writes_only_that_bridge_profile(self):
        self.profile("claude-agent").mkdir(parents=True)
        self.profile("gc").mkdir()
        result = self.run_login(account="claude-agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        grant = json.loads(
            (self.profile("claude-agent") / ".credentials.json").read_text()
        )
        self.assertEqual(grant["authenticated"], "native")
        self.assertEqual(grant["home"], str(self.target))
        self.assertFalse((self.profile("gc") / ".credentials.json").exists())
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.operator, True), {"existing": "native"})

    def test_missing_claude_account_profile_is_not_created(self):
        result = self.run_login(account="claude-agent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no profile", result.stderr)
        self.assertNotIn("LOGIN_READY", result.stdout)
        self.assertFalse(self.profile("claude-agent").exists())
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})

    def test_invalid_claude_account_name_is_rejected(self):
        (self.target / ".config/pi-claude-bridge").mkdir(parents=True)
        result = self.run_login(account="../pi-claude-bridge")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Claude account", result.stderr)
        self.assertNotIn("LOGIN_READY", result.stdout)
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})

    def test_account_is_rejected_for_pi_providers(self):
        self.profile("claude-agent").mkdir(parents=True)
        result = self.run_login("openai-codex", account="claude-agent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only to the claude provider", result.stderr)
        self.assertNotIn("LOGIN_READY", result.stdout)
        self.assertEqual(self.read_store(self.target, False), {"existing": "pi"})

    def test_failed_native_login_preserves_both_stores(self):
        result = self.run_login(confirmation="cancel\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("23", result.stderr)
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.target, False), {"existing": "pi"})

    def test_missing_native_executable_does_not_run_pi(self):
        (self.target / ".local/bin/claude").unlink()
        result = self.run_login()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Claude", result.stderr)
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.target, False), {"existing": "pi"})

    def test_removed_pi_login_name_is_not_a_compatibility_alias(self):
        result = subprocess.run(
            [str(self.binary), "pi-login", "claude"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_store(self.operator, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})

    def spawn_tty_login(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            [str(self.binary), "login", "claude", "--user", "allison"],
            env=self.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 10
        self.addCleanup(self.stop_tty_login, process)
        while b"LOGIN_READY" not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 4096))
        self.assertIn(b"LOGIN_READY", output)
        return master, process

    def stop_tty_login(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)

    def test_public_login_preserves_real_tty_input_and_output(self):
        master, process = self.spawn_tty_login()
        os.write(master, b"confirm\n")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertTrue(self.read_store(self.target, True)["tty"])

    def test_tty_cancellation_preserves_existing_credentials(self):
        _, process = self.spawn_tty_login()
        os.killpg(process.pid, signal.SIGINT)
        self.assertNotEqual(process.wait(timeout=10), 0)
        self.assertEqual(self.read_store(self.target, True), {"existing": "native"})
        self.assertEqual(self.read_store(self.target, False), {"existing": "pi"})


if __name__ == "__main__":
    unittest.main()

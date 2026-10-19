"""Local subprocess deployment proof; SSH, passwd and authsudo are fixtures.

No real UID switch, service operation, provider request or remote host is used.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeployUsersTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ra-deploy-users-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.operator = self.root / "operator"
        self.controller = self.root / "controller"
        (self.controller / ".local/bin").mkdir(parents=True)
        # Deliberately not /home/USERNAME: deployment must use passwd's home.
        self.target = self.root / "accounts/custom-home"
        self.events = self.root / "events.jsonl"
        self.profile = {
            "ssh": "fixture-server",
            "home": str(self.operator),
            "wrap": [],
            "windows_shell": False,
            "cargo_build_jobs": "2",
            "deploy_users": ["researcher"],
        }
        self.write_operator_config()
        config = self.target / ".config/remote-agent"
        config.mkdir(parents=True)
        (config / "remote-hosts.json").write_text(
            json.dumps({"agent-server": {"cargo_build_jobs": "1"}}, indent=3) + "\n"
        )
        (config / "remote-projects.json").write_text(
            '{\n "target-only": {"remote": "target-project"}\n}\n'
        )
        (self.target / "target-project").mkdir()
        self.installed = "Projects/cli/remote-agent"
        stale = self.target / self.installed / "remote-agent-worker.py"
        stale.parent.mkdir(parents=True)
        stale.write_text(
            "import json,sys\njson.load(sys.stdin)\n"
            "print(json.dumps({'error':'unknown action: history', 'revision':'fa9056ae'}))\n"
            "sys.exit(1)\n"
        )
        self.state = self.target / ".local/state/remote-agent/remote-agent-demo"
        self.state.mkdir(parents=True, mode=0o700)
        self.state.parent.chmod(0o700)
        (self.state / "session.json").write_text(
            json.dumps(
                {
                    "repo": "target-only",
                    "agent": "pi",
                    "backend": "dtach",
                    "conversation": "saved-conversation",
                }
            )
        )
        (self.state / "session.json").chmod(0o600)
        (self.state / "runtime.json").write_text('{"fixture":"saved runtime"}\n')
        (self.state / "runtime.json").chmod(0o600)
        self.conversation = self.target / ".config/pi/agent/sessions/saved.jsonl"
        self.conversation.parent.mkdir(parents=True)
        self.conversation.write_bytes(b'{"id":"saved-conversation","text":"keep me"}\n')
        self.history = "\x1b[32msaved history\x1b[0m\n"
        for home in (self.operator, self.target):
            self.write_command(
                home,
                "systemctl",
                f"""import json,os,pathlib,sys
if sys.argv[1:] not in (['show','agents.slice','-p','FragmentPath','--value'], ['--user','show','agents.slice','-p','FragmentPath','--value']):
 raise SystemExit('fixture forbids service changes: '+repr(sys.argv))
with pathlib.Path({str(self.events)!r}).open('a') as f:
 f.write(json.dumps({{'event':'slice','home':os.environ['HOME'], 'env':dict(os.environ)}})+'\\n')
print('/etc/systemd/system/agents.slice')
""",
            )
        self.write_command(
            self.target,
            "pi",
            f"""import json,os,pathlib,sys
if sys.argv[1:] != ['--export-terminal','saved-conversation','--export-columns','93']:
 raise SystemExit('fixture forbids agent lifecycle operations')
with pathlib.Path({str(self.events)!r}).open('a') as f:
 f.write(json.dumps({{'event':'history','env':dict(os.environ), 'cwd':os.getcwd()}})+'\\n')
print({self.history!r},end='')
""",
        )
        self.write_command(
            self.operator,
            "authsudo",
            f"""import json,os,pathlib,sys
with pathlib.Path({str(self.events)!r}).open('a') as f:
 f.write(json.dumps({{'event':'authorize','user':sys.argv[2]}})+'\\n')
if sys.argv[2]=='denied':
 raise SystemExit('authsudo: user denied')
os.execv(sys.argv[3],sys.argv[3:])
""",
        )
        self.write_command(
            self.operator,
            "ssh",
            f"""import os,pwd,shlex,sys,types
args=sys.argv[1:]
while args[0]=='-o': args=args[2:]
command=shlex.split(args[1])
def account(name):
 if name not in ('researcher','denied'): raise KeyError('unknown account: '+name)
 return types.SimpleNamespace(pw_name=name,pw_dir={str(self.target)!r},pw_uid=4321,pw_shell='/bin/sh')
pwd.getpwnam=account
os.environ['HOME']={str(self.operator)!r}
os.environ['PI_CODING_AGENT_DIR']={str(self.operator / "custom-pi")!r}
# Run the actual remote program, with only account lookup replaced.
sys.argv=command[2:]
exec(compile(command[2],'<fixture-ssh>','exec'))
""",
        )
        self.env = {
            **os.environ,
            "HOME": str(self.controller),
            "USER": "operator",
            "LOGNAME": "operator",
            "PATH": str(self.operator / ".local/bin") + os.pathsep + os.environ["PATH"],
            "XDG_RUNTIME_DIR": "/operator/runtime",
            "PI_CODING_AGENT_DIR": str(self.controller / "custom-pi"),
            "ADMIN_TEST_CREDENTIAL": "not-a-secret",
            "TMUX": "/operator/socket,123,0",
        }

    def write_command(self, home, name, code):
        path = home / ".local/bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o700)

    def write_operator_config(self):
        config = self.controller / ".config/remote-agent"
        config.mkdir(parents=True, exist_ok=True)
        (config / "remote-hosts.json").write_text(
            json.dumps({"agent-server": self.profile})
        )
        (config / "remote-projects.json").write_text(
            '{"operator-only":{"remote":"operator-project"}}\n'
        )

    def deploy(self):
        return subprocess.run(
            [sys.executable, str(ROOT / "deploy.py")],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def history_request(self):
        helper = self.operator / self.installed / "remote-agent-user.py"
        bootstrap = (
            "import pwd,types,runpy,sys; "
            f"pwd.getpwnam=lambda name: types.SimpleNamespace(pw_name=name,pw_dir={str(self.target)!r},pw_uid=4321,pw_shell='/bin/sh'); "
            f"sys.argv=[{str(helper)!r},'researcher','agent-server']; "
            f"runpy.run_path({str(helper)!r},run_name='__main__')"
        )
        return subprocess.run(
            [sys.executable, "-c", bootstrap],
            env=self.env,
            input=json.dumps({"action": "history", "name": "demo", "columns": 93}),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def snapshot(self):
        paths = [
            self.target / ".config/remote-agent",
            self.state.parent,
            self.state,
            self.conversation.parent,
            self.conversation,
            *self.state.iterdir(),
            *(self.target / ".config/remote-agent").iterdir(),
        ]
        return {
            str(p): (
                p.stat().st_mode,
                p.stat().st_ino,
                p.stat().st_mtime_ns,
                p.read_bytes() if p.is_file() else None,
            )
            for p in paths
        }

    def assert_identity(self, env):
        for key, value in {
            "HOME": str(self.target),
            "USER": "researcher",
            "LOGNAME": "researcher",
            "XDG_RUNTIME_DIR": "/run/user/4321",
            "PI_CODING_AGENT_DIR": str(self.target / ".config/pi/agent"),
        }.items():
            self.assertEqual(env[key], value)
        self.assertNotIn("ADMIN_TEST_CREDENTIAL", env)
        self.assertNotIn("TMUX", env)

    def test_upgrades_stale_worker_and_exports_history_without_changing_target_state(
        self,
    ):
        before = self.snapshot()
        stale_bytes = (
            self.target / self.installed / "remote-agent-worker.py"
        ).read_bytes()
        stale = subprocess.run(
            [
                sys.executable,
                str(self.target / self.installed / "remote-agent-worker.py"),
            ],
            input='{"action":"history"}',
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(stale.returncode, 1)
        self.assertEqual(json.loads(stale.stdout)["error"], "unknown action: history")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        worker = self.target / self.installed / "remote-agent-worker.py"
        self.assertNotEqual(worker.read_bytes(), stale_bytes)
        history = self.history_request()
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(
            json.loads(history.stdout), {"available": True, "history": self.history}
        )
        self.assertEqual(self.snapshot(), before)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual(
            [event["home"] for event in events if event["event"] == "slice"],
            [str(self.operator)] * 2 + [str(self.target)] * 2,
        )
        for event in events:
            if event["event"] == "history" or event.get("home") == str(self.target):
                self.assert_identity(event["env"])
        self.assertEqual(events[-1]["cwd"], str(self.target / "target-project"))
        self.assertEqual(
            (self.operator / "data/remote-agents/controller-revision.txt").read_bytes(),
            (self.target / "data/remote-agents/controller-revision.txt").read_bytes(),
        )

    def test_unconfigured_deploy_keeps_default_behavior_and_leaves_other_worker_stale(
        self,
    ):
        for setting in (None, []):
            with self.subTest(setting=setting):
                if setting is None:
                    self.profile.pop("deploy_users")
                else:
                    self.profile["deploy_users"] = setting
                self.write_operator_config()
                before = self.snapshot()
                stale = (
                    self.target / self.installed / "remote-agent-worker.py"
                ).read_bytes()
                result = self.deploy()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(
                    (
                        self.target / self.installed / "remote-agent-worker.py"
                    ).read_bytes(),
                    stale,
                )
                for name in ("remote-hosts.json", "remote-projects.json"):
                    self.assertEqual(
                        (self.operator / ".config/remote-agent" / name).read_bytes(),
                        (
                            json.dumps({"agent-server": self.profile})
                            if name == "remote-hosts.json"
                            else '{"operator-only":{"remote":"operator-project"}}\n'
                        ).encode(),
                    )
                self.assertTrue(
                    (self.controller / ".local/bin/remote-agent").is_symlink()
                )

    def test_missing_target_maps_fail_without_copying_operator_maps(self):
        for name in ("remote-hosts.json", "remote-projects.json"):
            with self.subTest(name=name):
                path = self.target / ".config/remote-agent" / name
                original = path.read_bytes()
                stale = (
                    self.target / self.installed / "remote-agent-worker.py"
                ).read_bytes()
                path.unlink()
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertIn("researcher", result.stderr)
                self.assertFalse(path.exists())
                self.assertEqual(
                    (
                        self.target / self.installed / "remote-agent-worker.py"
                    ).read_bytes(),
                    stale,
                )
                path.write_bytes(original)

    def test_unknown_and_unauthorized_accounts_fail_after_default_deploy(self):
        for user in ("missing", "denied"):
            with self.subTest(user=user):
                self.profile["deploy_users"] = [user]
                self.write_operator_config()
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(user, result.stderr)
                self.assertTrue(
                    (self.operator / self.installed / "remote-agent-user.py").exists()
                )

    def test_wrapped_or_windows_cross_user_deploy_is_rejected(self):
        for key, value in (("wrap", ["wsl.exe", "--exec"]), ("windows_shell", True)):
            with self.subTest(key=key):
                self.profile.update(wrap=[], windows_shell=False)
                self.profile[key] = value
                self.write_operator_config()
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("native Linux", result.stderr)
                self.assertFalse(self.events.exists())

    def test_deploy_users_must_be_a_list_of_usernames(self):
        for value in ("researcher", [1], [""], ["researcher;bad"]):
            with self.subTest(value=value):
                self.profile["deploy_users"] = value
                self.write_operator_config()
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("deploy_users", result.stderr)
                self.assertFalse(self.events.exists())


if __name__ == "__main__":
    unittest.main()

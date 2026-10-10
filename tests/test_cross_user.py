"""Cross-account controller behavior with real processes and account fixtures.

The account directory and authorization command are fixtures. Live acceptance
separately proves the real Unix UID/tmux boundary without model calls.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CrossUserControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.admin = self.root / "operator"
        self.target = self.root / "researcher"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.events = self.root / "events.jsonl"
        self.worker = "Projects/cli/remote-agent/remote-agent-worker.py"
        hosts = {
            "agent-server": {
                "ssh": "fixture-server",
                "home": str(self.admin),
                "wrap": [],
                "windows_shell": False,
                "cargo_build_jobs": "2",
            },
            "wrapped": {
                "ssh": "fixture-wrapped",
                "home": str(self.admin),
                "wrap": ["wsl.exe", "--exec"],
                "windows_shell": True,
                "cargo_build_jobs": "2",
            },
        }
        for home, name in ((self.admin, "operator-only"), (self.target, "target-only")):
            config = home / ".config/remote-agent"
            config.mkdir(parents=True)
            (config / "remote-hosts.json").write_text(json.dumps(hosts))
            (config / "remote-projects.json").write_text(json.dumps({"session": name}))
            worker = home / self.worker
            worker.parent.mkdir(parents=True)
            worker.write_text(f"""import json,os,pathlib,sys
home=pathlib.Path.home()
name=json.loads((home/'.config/remote-agent/remote-projects.json').read_text())['session']
identity={{key:os.environ.get(key) for key in ('HOME','USER','LOGNAME','XDG_RUNTIME_DIR','PI_CODING_AGENT_DIR')}}
identity['admin_credential_present']='ADMIN_TEST_CREDENTIAL' in os.environ
identity['admin_tmux_present']='TMUX' in os.environ
if '--attach' in sys.argv:
 with pathlib.Path({str(self.events)!r}).open('a') as f:
  f.write(json.dumps({{'event':'attach','name':name,'identity':identity}})+'\\n')
 print(json.dumps({{'attached':name}}))
else:
 request=json.load(sys.stdin)
 if request['action']=='list':
  print(json.dumps({{'sessions':[{{'name':name,'identity':identity}}]}}))
 elif request['action']=='status':
  print(json.dumps({{'session':name}}))
""")
        helper = self.admin / "Projects/cli/remote-agent/remote-agent-user.py"
        helper.write_text(f"""import importlib.util,pwd,sys,types
spec=importlib.util.spec_from_file_location('account_worker',{str(ROOT / "remote-agent-user.py")!r})
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
def account(name):
 if name not in ('researcher','denied'):
  raise KeyError(name)
 return types.SimpleNamespace(pw_name=name,pw_dir={str(self.target)!r},pw_uid=4321,pw_shell='/bin/sh')
module.pwd.getpwnam=account
sys.exit(module.main())
""")
        self.write_command(
            "authsudo",
            """import os,sys
if sys.argv[2]=='denied':
 print('authsudo: user not authorized',file=sys.stderr)
 sys.exit(1)
os.execv(sys.argv[3],sys.argv[3:])
""",
        )
        self.write_command(
            "ssh",
            """import os,sys
args=sys.argv[1:]
while args and args[0]=='-o':
 args=args[2:]
args=args[1:]
os.execvp(args[0],args)
""",
        )
        self.write_command(
            "mosh",
            f"""import os,pathlib,subprocess,sys
count=pathlib.Path({str(self.root / "mosh-count")!r})
n=int(count.read_text()) if count.exists() else 0
count.write_text(str(n+1))
args=sys.argv[sys.argv.index('--')+2:]
r=subprocess.run(args)
if n==0 and os.environ.get('FIXTURE_DROP_FIRST')=='1':
 sys.exit(255)
sys.exit(r.returncode)
""",
        )
        self.env = {
            **os.environ,
            "HOME": str(self.admin),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "ADMIN_TEST_CREDENTIAL": "fixture-not-a-secret",
            "TMUX": "/operator/socket,123,0",
        }

    def write_command(self, name, content):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + content)
        path.chmod(0o700)

    def invoke(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "remote-agent.py"), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def assert_target_identity(self, identity):
        self.assertEqual(identity["HOME"], str(self.target))
        self.assertEqual(identity["USER"], "researcher")
        self.assertEqual(identity["LOGNAME"], "researcher")
        self.assertEqual(identity["XDG_RUNTIME_DIR"], "/run/user/4321")
        self.assertEqual(
            identity["PI_CODING_AGENT_DIR"], str(self.target / ".config/pi/agent")
        )
        self.assertFalse(identity["admin_credential_present"])
        self.assertFalse(identity["admin_tmux_present"])

    def test_list_user_reads_target_mapping_and_clears_admin_environment(self):
        result = self.invoke("list", "--user", "researcher")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["sessions"]), 1)
        session = data["sessions"][0]
        self.assertEqual(session["name"], "target-only")
        self.assertEqual(session["user"], "researcher")
        self.assertEqual(session["host"], "agent-server")
        self.assert_target_identity(session["identity"])

    def test_default_list_keeps_operator_account(self):
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        session = json.loads(result.stdout)["sessions"][0]
        self.assertEqual(session["name"], "operator-only")
        self.assertEqual(session["identity"]["HOME"], str(self.admin))
        self.assertNotIn("user", session)

    def test_attach_user_preserves_account_on_reconnect_and_detach(self):
        self.env["FIXTURE_DROP_FIRST"] = "1"
        result = self.invoke("attach", "target-only", "--user", "researcher")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event["name"], "target-only")
            self.assert_target_identity(event["identity"])
        self.assertIn("reconnecting", result.stderr)
        self.assertEqual((self.root / "mosh-count").read_text(), "2")

    def test_authorization_failure_is_explicit_without_mosh_retry(self):
        result = self.invoke("attach", "target-only", "--user", "denied")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not authorized", result.stderr)
        self.assertFalse((self.root / "mosh-count").exists())
        self.assertFalse(self.events.exists())

    def test_unknown_user_returns_account_context(self):
        result = self.invoke("list", "--user", "missing")
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data["sessions"], [])
        self.assertIn("missing", data["errors"][0])

    def test_wrapped_host_rejects_user_override(self):
        result = self.invoke("list", "--host", "wrapped", "--user", "researcher")
        self.assertEqual(result.returncode, 1)
        self.assertIn("native Linux", json.loads(result.stdout)["errors"][0])
        self.assertFalse(self.events.exists())

    def test_user_option_rejects_invalid_names_and_other_actions(self):
        for args in (
            ("list", "--user", "researcher;bad"),
            ("status", "target-only", "--user", "researcher"),
            ("start", "project", "--user", "researcher"),
        ):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(self.events.exists())


if __name__ == "__main__":
    unittest.main()

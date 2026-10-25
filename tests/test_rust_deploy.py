"""Rust deployment with real files/processes and fixture SSH/native build tools.

No network, UID switch, real Rust compilation, or service operation is used.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINUX = "x86_64-unknown-linux-gnu"
MAC = "aarch64-apple-darwin"


class RustDeployTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="rust-deploy-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for name in ("deploy.py", "deploy.sh"):
            shutil.copy2(ROOT / name, self.repo / name)
        (self.repo / "Cargo.toml").write_text('[package]\nname="remote-agent-rs"\n')
        (self.repo / "Cargo.lock").write_text("# concrete locked source fixture\n")
        (self.repo / "src").mkdir()
        (self.repo / "src/main.rs").write_text("fn main() {}\n")
        self.client = self.root / "client"
        self.server = self.root / "server"
        self.other = self.root / "passwd-home"
        self.events = self.root / "builds.jsonl"
        self.homes = (self.client, self.server, self.other)
        self.profile = {
            "ssh": "fixture",
            "home": str(self.server),
            "wrap": [],
            "windows_shell": False,
            "deploy_users": ["researcher"],
        }
        for index, home in enumerate(self.homes):
            config = home / ".config/remote-agent"
            config.mkdir(parents=True)
            (config / "remote-hosts.json").write_text(
                json.dumps({"agent-server": self.profile}, indent=index + 1) + "\n"
            )
            (config / "remote-projects.json").write_text(
                '{ "project-' + str(index) + '": {} }\n'
            )
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local/bin/remote-agent").write_bytes(b"legacy untouched")
        helper = self.server / "Projects/cli/remote-agent/remote-agent-user.py"
        helper.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "remote-agent-user.py", helper)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.command(
            self.bin,
            "rustc",
            f"""import os
print('rustc fixture\\nhost: '+(os.environ.get('SERVER_TARGET', {LINUX!r}) if os.environ['HOME']=={str(self.server)!r} else os.environ.get('CLIENT_TARGET', {LINUX!r})))
""",
        )
        self.command(
            self.bin,
            "cargo",
            f"""import json,os,pathlib,sys
root=pathlib.Path.cwd()
assert (root/'Cargo.lock').read_text() == '# concrete locked source fixture\\n'
assert (root/'src/main.rs').read_text() == 'fn main() {{}}\\n'
assert '--locked' in sys.argv
assert sys.argv[sys.argv.index('--bin')+1]=='remote-agent-rs'
target=sys.argv[sys.argv.index('--target')+1]
with pathlib.Path({str(self.events)!r}).open('a') as out:
 out.write(json.dumps({{'home':os.environ['HOME'],'target':target}})+'\\n')
if os.environ.get('FAIL_BUILD_HOME')==os.environ['HOME']: sys.exit(37)
output=root/'target'/target/'release/remote-agent-rs'
output.parent.mkdir(parents=True,exist_ok=True)
output.write_bytes(('native:'+target+':'+os.environ.get('BUILD_TAG','one')).encode())
output.chmod(0o700)
""",
        )
        self.command(
            self.server / ".local/bin",
            "authsudo",
            """import os,sys
sys.argv[-1] = ('import platform; platform.system=lambda: "Linux"; '
                + 'platform.machine=lambda: '+repr(os.environ.get('SERVER_ARCH','x86_64'))+'; '+sys.argv[-1])
os.execv(sys.argv[3],sys.argv[3:])
""",
        )
        self.command(
            self.bin,
            "ssh",
            f"""import os,platform,pwd,shlex,sys,types
args=sys.argv[1:]
while args[0]=='-o': args=args[2:]
program=shlex.split(args[1])
os.environ['HOME']={str(self.server)!r}
os.environ['PATH']={str(self.server / ".local/bin")!r}+os.pathsep+os.environ['PATH']
platform.system=lambda: 'Linux'
platform.machine=lambda: os.environ.get('SERVER_ARCH','x86_64')
def account(name):
 if name!='researcher': raise KeyError(name)
 return types.SimpleNamespace(pw_name=name,pw_dir={str(self.other)!r},pw_uid=4321,pw_shell='/bin/sh')
pwd.getpwnam=account
exec(compile(program[2],'<fixture-ssh>','exec'))
""",
        )
        self.env = {
            **os.environ,
            "HOME": str(self.client),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }
        self.before = self.maps()

    def command(self, directory, name, code):
        path = directory / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o700)

    def maps(self):
        return {
            path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for home in self.homes
            for path in (home / ".config/remote-agent").iterdir()
        }

    def deploy(self, mac=False):
        if mac:
            command = [
                sys.executable,
                "-c",
                "import platform,runpy,sys; "
                "platform.system=lambda:'Darwin'; platform.machine=lambda:'arm64'; "
                f"sys.argv=['deploy.py','--rust']; runpy.run_path({str(self.repo / 'deploy.py')!r},run_name='__main__')",
            ]
            self.env["CLIENT_TARGET"] = MAC
        else:
            command = [str(self.repo / "deploy.sh"), "--rust"]
        return subprocess.run(
            command, env=self.env, text=True, capture_output=True, timeout=20
        )

    def builds(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def assert_installed(self, home, target, tag="one"):
        content = f"native:{target}:{tag}".encode()
        digest = hashlib.sha256(content).hexdigest()
        root = home / ".local/share/remote-agent-rs"
        binary = root / digest / "remote-agent-rs"
        link = home / ".local/bin/remote-agent-rs"
        self.assertEqual(link.readlink(), binary)
        self.assertEqual(binary.read_bytes(), content)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(binary.parent.stat().st_mode & 0o777, 0o500)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o500)
        self.assertEqual(
            (home / ".local/bin/remote-agent").read_bytes(), b"legacy untouched"
        )
        return binary

    def test_linux_build_once_installs_all_accounts_preserving_maps_and_old_versions(
        self,
    ):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.builds(), [{"home": str(self.client), "target": LINUX}])
        old = [self.assert_installed(home, LINUX) for home in self.homes]
        old_stats = [(p.stat().st_ino, p.stat().st_mtime_ns) for p in old]
        self.env["BUILD_TAG"] = "two"
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        for home in self.homes:
            self.assert_installed(home, LINUX, "two")
        for path, expected in zip(old, old_stats):
            self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), expected)
            self.assertEqual(path.read_bytes(), f"native:{LINUX}:one".encode())
        self.assertEqual(self.maps(), self.before)

    def test_macos_client_builds_linux_source_once_and_redistributes(self):
        result = self.deploy(mac=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed(self.client, MAC)
        for home in (self.server, self.other):
            self.assert_installed(home, LINUX)
        self.assertEqual(
            self.builds(),
            [
                {"home": str(self.client), "target": MAC},
                {"home": str(self.server), "target": LINUX},
            ],
        )
        self.assertEqual(self.maps(), self.before)

    def test_linux_architecture_mismatch_builds_server_from_locked_source(self):
        self.env.update(
            SERVER_ARCH="aarch64", SERVER_TARGET="aarch64-unknown-linux-gnu"
        )
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed(self.client, LINUX)
        for home in (self.server, self.other):
            self.assert_installed(home, "aarch64-unknown-linux-gnu")
        self.assertEqual(len(self.builds()), 2)

    def test_missing_maps_fail_without_seeding_default_or_other_account(self):
        for home in (self.client, self.server, self.other):
            with self.subTest(home=home):
                path = home / ".config/remote-agent/remote-projects.json"
                content = path.read_bytes()
                path.unlink()
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("remote-projects.json", result.stderr)
                self.assertFalse(path.exists())
                path.write_bytes(content)

    def test_refuses_unmanaged_file_and_foreign_or_dangling_symlink(self):
        for home in self.homes:
            for kind in ("file", "foreign", "dangling"):
                with self.subTest(home=home, kind=kind):
                    path = home / ".local/bin/remote-agent-rs"
                    if path.is_symlink() or path.exists():
                        path.unlink()
                    foreign = home / "foreign"
                    foreign.write_bytes(b"keep foreign")
                    if kind == "file":
                        path.write_bytes(b"keep unmanaged")
                    else:
                        path.symlink_to(
                            foreign if kind == "foreign" else home / "missing"
                        )
                    result = self.deploy()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Not replacing", result.stderr)
                    if kind == "file":
                        self.assertEqual(path.read_bytes(), b"keep unmanaged")
                    else:
                        self.assertEqual(
                            path.readlink(),
                            foreign if kind == "foreign" else home / "missing",
                        )
                    path.unlink()
        self.assertEqual(self.maps(), self.before)

    def test_build_failures_do_not_select_another_build_route(self):
        for home, expected_count in ((self.client, 1), (self.server, 2)):
            with self.subTest(home=home):
                self.events.unlink(missing_ok=True)
                self.env["FAIL_BUILD_HOME"] = str(home)
                result = self.deploy(mac=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.builds()), expected_count)
                for account in self.homes:
                    self.assertFalse((account / ".local/bin/remote-agent-rs").exists())

    def test_target_metadata_must_match_native_platform(self):
        self.env["CLIENT_TARGET"] = MAC
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target", result.stderr.lower())
        self.assertFalse(self.events.exists())
        self.env.update(CLIENT_TARGET=LINUX, SERVER_ARCH="aarch64", SERVER_TARGET=LINUX)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("target", result.stderr.lower())
        self.assertEqual(len(self.builds()), 1)


if __name__ == "__main__":
    unittest.main()

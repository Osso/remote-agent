#!/usr/bin/env python3
"""Deploy the local controller and the worker to a selected host."""

import argparse
import base64
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config/remote-agent"
CONFIG_FILES = ("remote-hosts.json", "remote-projects.json")
HOSTS = json.loads((CONFIG / "remote-hosts.json").read_text())
DEFAULT_HOST = "agent-server"
FILES = (
    "remote-agent.py",
    "remote-agent-worker.py",
    "remote-agent-user.py",
    "agents.slice",
    "tests/test_remote_agent.py",
    "tests/test_dtach.py",
    "tests/test_dtach_scope.py",
    "tests/test_terminal_colors.py",
    "tests/test_history.py",
    "tests/test_history_attach.py",
    "tests/test_resize_attach.py",
    "tests/test_mosh_resize.py",
    "tests/fixtures/mosh-resize.ts",
    "tests/history_scrollback_fixture.cjs",
    "run-tests.sh",
    "tests/test_cross_user.py",
    "tests/test_periodic_runtime.py",
    "tests/fixtures/periodic-runtime-provider.ts",
    "README.md",
)
REMOTE_PROGRAM = r"""import base64,io,json,os,pathlib,subprocess,sys,tarfile
request=json.load(sys.stdin)
home=pathlib.Path.home()
if home!=pathlib.Path(request['home']):
 raise RuntimeError('Expected remote home '+request['home']+', got '+str(home))
config=home/'.config/remote-agent'
if request.get('preserve_config'):
 for name in ('remote-hosts.json','remote-projects.json'):
  (config/name).read_bytes()
else:
 config.mkdir(parents=True,exist_ok=True,mode=0o700)
 config.chmod(0o700)
 for name in ('remote-hosts.json','remote-projects.json'):
  path=config/name
  content=base64.b64decode(request['config'][name])
  path.touch(mode=0o600,exist_ok=True)
  path.chmod(0o600)
  path.write_bytes(content)
destination=home/'Projects/cli/remote-agent'
destination.mkdir(parents=True,exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(request['archive'])),mode='r:') as archive:
 archive.extractall(destination,filter='data')
def link(path,source):
 if path.exists() and not path.is_symlink():
  raise RuntimeError('Not replacing unmanaged '+str(path))
 if path.is_symlink() and path.readlink()!=source:
  raise RuntimeError('Not replacing foreign symlink '+str(path))
 if not path.is_symlink():
  path.symlink_to(source)
(home/'.local/bin').mkdir(parents=True,exist_ok=True)
link(home/'.local/bin/wl-paste',destination/'remote-agent-worker.py')
def slice_fragment(user):
 command=['systemctl']+(['--user'] if user else [])+['show','agents.slice','-p','FragmentPath','--value']
 return subprocess.run(command,text=True,capture_output=True,check=True).stdout.strip()
system_slice=slice_fragment(False)
user_slice=slice_fragment(True)
slice_unit=home/'.config/systemd/user/agents.slice'
if system_slice or user_slice or slice_unit.exists() or slice_unit.is_symlink():
 print('Preserving host agents.slice: '+(system_slice or user_slice or str(slice_unit)))
else:
 slice_unit.parent.mkdir(parents=True,exist_ok=True)
 slice_unit.write_bytes((destination/'agents.slice').read_bytes())
 subprocess.run(['systemctl','--user','daemon-reload'],check=True)
revision_file=home/'data/remote-agents/controller-revision.txt'
revision_file.parent.mkdir(parents=True,exist_ok=True)
revision_file.write_text(request['revision']+'\n')
print('remote-agent deployed: '+request['revision'])
"""

USER_DEPLOY_PROGRAM = r"""import json,os,pathlib,pwd,runpy,subprocess,sys
request=json.load(sys.stdin)
user=request.pop('user')
try:
 account=pwd.getpwnam(user)
 helper=pathlib.Path(request['home'])/'Projects/cli/remote-agent/remote-agent-user.py'
 environment=runpy.run_path(str(helper))['build_user_environment'](account,os.environ)
 request['home']=account.pw_dir
 program=request.pop('program')
 command=['authsudo','-u',account.pw_name,'/usr/bin/env','-i',
          *(name+'='+value for name,value in environment.items()),
          '/usr/bin/python3','-c',program]
 subprocess.run(command,input=json.dumps(request),text=True,check=True)
except (KeyError,OSError,subprocess.CalledProcessError) as error:
 print('remote-agent: deployment as '+user+' failed: '+str(error),file=sys.stderr)
 sys.exit(1)
"""


# Shared by the native client installer and the SSH deployment payload.
RUST_SUPPORT = r"""import base64,hashlib,io,json,os,pathlib,platform,re,subprocess,sys,tarfile,tempfile

def native_platform():
    system = platform.system()
    machine = platform.machine()
    arch = {'x86_64': 'x86_64', 'amd64': 'x86_64',
            'aarch64': 'aarch64', 'arm64': 'aarch64'}.get(machine.lower())
    if system not in ('Linux', 'Darwin') or arch is None:
        raise RuntimeError('Unsupported native platform: '+system+'/'+machine)
    return {'os': system, 'arch': arch}

def validate_target(target, native):
    suffix = '-unknown-linux-gnu' if native['os'] == 'Linux' else '-apple-darwin'
    if target != native['arch']+suffix:
        raise RuntimeError('Rust target '+target+' does not match native platform '+str(native))

def native_target():
    metadata = subprocess.check_output(['rustc', '-vV'], text=True)
    hosts = [line[6:] for line in metadata.splitlines() if line.startswith('host: ')]
    if len(hosts) != 1:
        raise RuntimeError('Missing unique rustc host target metadata')
    validate_target(hosts[0], native_platform())
    return hosts[0]

def require_maps(home):
    for name in ('remote-hosts.json', 'remote-projects.json'):
        (home/'.config/remote-agent'/name).read_bytes()

def build_binary(source):
    target = native_target()
    subprocess.run(['cargo', 'build', '--locked', '--release', '--bin',
                    'remote-agent-rs', '--target', target], cwd=source,
                   stdout=sys.stderr, check=True,
                   env={**os.environ, 'CARGO_TARGET_DIR': str(source/'target')})
    content = (source/'target'/target/'release/remote-agent-rs').read_bytes()
    return {'target': target, 'binary': base64.b64encode(content).decode(),
            'sha256': hashlib.sha256(content).hexdigest()}

def check_link(link, root):
    if link.is_symlink():
        old = link.readlink()
        if (old.parent.parent != root or old.name != 'remote-agent-rs'
                or not re.fullmatch('[0-9a-f]{64}', old.parent.name)
                or old.parent.is_symlink() or old.is_symlink() or not old.is_file()
                or hashlib.sha256(old.read_bytes()).hexdigest() != old.parent.name):
            raise RuntimeError('Not replacing foreign symlink '+str(link))
    elif link.exists():
        raise RuntimeError('Not replacing unmanaged '+str(link))

def install_binary(home, payload, command):
    require_maps(home)
    validate_target(payload['target'], native_platform())
    content = base64.b64decode(payload['binary'], validate=True)
    digest = hashlib.sha256(content).hexdigest()
    if digest != payload['sha256']:
        raise RuntimeError('Rust binary content hash mismatch')
    root = home/'.local/share/remote-agent-rs'
    link = home/'.local/bin'/command
    if root.is_symlink():
        raise RuntimeError('Not replacing foreign symlink '+str(root))
    check_link(link, root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    version = root/digest
    binary = version/'remote-agent-rs'
    if version.is_symlink():
        raise RuntimeError('Not replacing foreign symlink '+str(version))
    if version.exists():
        if (binary.is_symlink() or binary.read_bytes() != content
                or version.stat().st_mode & 0o777 != 0o500
                or binary.stat().st_mode & 0o777 != 0o500):
            raise RuntimeError('Invalid immutable Rust installation '+str(version))
    else:
        version.mkdir(mode=0o700)
        with binary.open('xb') as output:
            output.write(content)
        binary.chmod(0o500)
        version.chmod(0o500)
    link.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.remote-agent-rs-', dir=link.parent)
    os.close(descriptor)
    temporary = pathlib.Path(temporary)
    temporary.unlink()
    try:
        temporary.symlink_to(binary)
        check_link(link, root)
        temporary.replace(link)
    finally:
        temporary.unlink(missing_ok=True)
    return {'installed': str(binary)}
"""

RUST_PROGRAM = (
    RUST_SUPPORT
    + r"""
def install_session_unit(home, content):
    # Template for the per-session user units that `start` enables.
    path = home/'.config/systemd/user/remote-agent-rs@.service'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)

request = json.load(sys.stdin)
home = pathlib.Path.home()
if home != pathlib.Path(request['home']):
    raise RuntimeError('Unexpected remote home '+str(home))
require_maps(home)
native = native_platform()
if native['os'] != 'Linux':
    raise RuntimeError('Rust server deployment requires native Linux')
if request['action'] == 'probe':
    result = native
elif request['action'] == 'build':
    with tempfile.TemporaryDirectory(prefix='remote-agent-rs-build-') as directory:
        source = pathlib.Path(directory)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(request['source'])), mode='r:') as archive:
            archive.extractall(source, filter='data')
        result = build_binary(source)
elif request['action'] == 'install':
    # The session unit template runs %h/.local/bin/remote-agent-rs.
    result = install_binary(home, request['payload'], 'remote-agent-rs')
    install_session_unit(home, request['unit'])
else:
    raise RuntimeError('Unknown Rust deployment action '+request['action'])
print(json.dumps(result))
"""
)


def bundle_rust_source():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name in ("Cargo.toml", "Cargo.lock", "src"):
            bundle.add(ROOT / name, arcname=name)
    return base64.b64encode(archive.getvalue()).decode()


def deploy_rust(host):
    profile = HOSTS[host]
    users = profile.get("deploy_users", [])
    if not isinstance(users, list) or any(
        not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user)
        for user in users
    ):
        raise ValueError("deploy_users must be a list of Unix usernames")
    if profile["wrap"] or profile["windows_shell"]:
        raise ValueError(
            "Rust deployment requires a native Linux server without wrapping"
        )
    key = "REMOTE_AGENT_" + host.upper().replace("-", "_") + "_SSH"
    target = os.environ.get(key, profile["ssh"])
    native = {}
    exec(RUST_SUPPORT, native)
    home = Path.home()
    native["require_maps"](home)

    def remote(action, user=None, **values):
        request = {"home": profile["home"], "action": action, **values}
        program = RUST_PROGRAM
        if user is not None:
            request.update(user=user, program=program)
            program = USER_DEPLOY_PROGRAM
        output = deploy_remote(profile, target, program, request, capture_output=True)
        return json.loads(output)

    server = remote("probe")
    for user in users:
        if remote("probe", user) != server:
            raise RuntimeError("Configured accounts have different server platforms")
    client = native["native_platform"]()
    payload = native["build_binary"](ROOT)
    server_payload = (
        payload if client == server else remote("build", source=bundle_rust_source())
    )
    native["install_binary"](home, payload, "remote-agent")
    unit = (ROOT / "remote-agent-rs@.service").read_text()
    remote("install", payload=server_payload, unit=unit)
    for user in users:
        remote("install", user, payload=server_payload, unit=unit)
    print("remote-agent installed locally; remote-agent-rs server installed on " + host)


def bundle_files():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name in FILES:
            bundle.add(ROOT / name, arcname=name, recursive=False)
    return base64.b64encode(archive.getvalue()).decode()


def deploy_worker(host=DEFAULT_HOST):
    profile = HOSTS[host]
    users = profile.get("deploy_users", [])
    if not isinstance(users, list) or any(
        not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user)
        for user in users
    ):
        raise ValueError("deploy_users must be a list of Unix usernames")
    if users and (profile["wrap"] or profile["windows_shell"]):
        raise ValueError("deploy_users requires a native Linux host without wrapping")
    key = "REMOTE_AGENT_" + host.upper().replace("-", "_") + "_SSH"
    target = os.environ.get(key, profile["ssh"])
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    request = {
        "archive": bundle_files(),
        "revision": revision,
        "home": profile["home"],
        "host": host,
    }
    deploy_remote(
        profile,
        target,
        REMOTE_PROGRAM,
        {
            **request,
            "config": {
                name: base64.b64encode((CONFIG / name).read_bytes()).decode()
                for name in CONFIG_FILES
            },
        },
    )
    for user in users:
        deploy_remote(
            profile,
            target,
            USER_DEPLOY_PROGRAM,
            {
                **request,
                "user": user,
                "preserve_config": True,
                "program": REMOTE_PROGRAM,
            },
        )


def deploy_remote(profile, target, program, request, capture_output=False):
    encoded = base64.b64encode(program.encode()).decode()
    argv = [
        *profile["wrap"],
        "python3",
        "-c",
        f"import base64;exec(base64.b64decode('{encoded}'))",
    ]
    remote = (
        subprocess.list2cmdline(argv) if profile["windows_shell"] else shlex.join(argv)
    )
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, remote],
        input=json.dumps(request),
        text=True,
        check=True,
        stdout=subprocess.PIPE if capture_output else None,
    )
    return result.stdout


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=HOSTS, default=DEFAULT_HOST)
    parser.add_argument(
        "--rust",
        action="store_true",
        help="Install the native client as remote-agent and the Linux server",
    )
    args = parser.parse_args()
    if args.rust:
        deploy_rust(args.host)
    else:
        deploy_worker(args.host)

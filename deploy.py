#!/usr/bin/env python3
"""Install the Rust client locally and the Rust session server on a selected host."""

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

USER_DEPLOY_PROGRAM = r"""import json,os,pwd,subprocess,sys
request=json.load(sys.stdin)
user=request.pop('user')
try:
 account=pwd.getpwnam(user)
 home=account.pw_dir
 environment={'HOME':home,'USER':account.pw_name,'LOGNAME':account.pw_name,
              'SHELL':account.pw_shell,'PATH':home+'/.local/bin:/usr/local/bin:/usr/bin:/bin',
              'XDG_RUNTIME_DIR':'/run/user/'+str(account.pw_uid),
              'PI_CODING_AGENT_DIR':home+'/.config/pi/agent'}
 for name,value in os.environ.items():
  if name in ('TERM','LANG','LANGUAGE') or name.startswith('LC_'):
   environment[name]=value
 request['home']=home
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

def install_maps(home, maps):
    # Only the default SSH account receives the operator's maps.
    config = home/'.config/remote-agent'
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.chmod(0o700)
    for name, content in maps.items():
        path = config/name
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_bytes(base64.b64decode(content))

def install_slice(home, content):
    # Session units run in agents.slice; never replace a host-provided one.
    def fragment(user):
        command = ['systemctl']+(['--user'] if user else [])+['show', 'agents.slice', '-p', 'FragmentPath', '--value']
        return subprocess.run(command, text=True, capture_output=True, check=True).stdout.strip()
    path = home/'.config/systemd/user/agents.slice'
    if fragment(False) or fragment(True) or path.exists() or path.is_symlink():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

request = json.load(sys.stdin)
home = pathlib.Path.home()
if home != pathlib.Path(request['home']):
    raise RuntimeError('Unexpected remote home '+str(home))
if 'maps' in request:
    install_maps(home, request['maps'])
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
    install_slice(home, request['slice'])
    install_session_unit(home, request['unit'])
else:
    raise RuntimeError('Unknown Rust deployment action '+request['action'])
print(json.dumps(result))
"""
)


def bundle_rust_source():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name in ("Cargo.toml", "Cargo.lock", "src", "vendor"):
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

    maps = {
        name: base64.b64encode((CONFIG / name).read_bytes()).decode()
        for name in CONFIG_FILES
    }
    server = remote("probe", maps=maps)
    for user in users:
        if remote("probe", user) != server:
            raise RuntimeError("Configured accounts have different server platforms")
    client = native["native_platform"]()
    payload = native["build_binary"](ROOT)
    server_payload = (
        payload if client == server else remote("build", source=bundle_rust_source())
    )
    native["install_binary"](home, payload, "remote-agent")
    files = {
        "unit": (ROOT / "remote-agent-rs@.service").read_text(),
        "slice": (ROOT / "agents.slice").read_text(),
    }
    remote("install", payload=server_payload, **files)
    for user in users:
        remote("install", user, payload=server_payload, **files)
    print("remote-agent installed locally; remote-agent-rs server installed on " + host)


def install_switcher():
    """Install only the local frontend; preserve remote clients, workers and maps."""
    source = ROOT / "agent-switcher"
    if not source.is_file() or not os.access(source, os.X_OK):
        raise RuntimeError("Missing executable switcher: " + str(source))
    destination = Path.home() / ".local/bin/agent-switcher"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        if destination.resolve() != source.resolve():
            raise RuntimeError("Not replacing foreign symlink " + str(destination))
    elif destination.exists():
        raise RuntimeError("Not replacing unmanaged " + str(destination))
    else:
        destination.symlink_to(source)
    print("agent-switcher installed locally: " + str(destination))


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
        "--switcher",
        action="store_true",
        help="Install only the local Pi session switcher",
    )
    args = parser.parse_args()
    if args.switcher:
        install_switcher()
    else:
        deploy_rust(args.host)

#!/usr/bin/env python3
"""Deploy the local controller and the worker to a selected host."""

import argparse
import base64
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config/remote-agent"
CONFIG_FILES = ("remote-hosts.json", "remote-projects.json")
HOSTS = json.loads((CONFIG / "remote-hosts.json").read_text())
DEFAULT_HOST = "agent-server"
FILES = (
    "remote-agent.py",
    "remote-agent-worker.py",
    "remote-agent-user.py",
    "periodic-checks.ts",
    "agents.slice",
    "tests/test_remote_agent.py",
    "tests/test_dtach.py",
    "tests/test_dtach_scope.py",
    "tests/test_terminal_colors.py",
    "tests/test_history.py",
    "tests/test_history_attach.py",
    "tests/test_cross_user.py",
    "tests/test_periodic_scheduler.mjs",
    "tests/periodic_scheduler_dispatch.mjs",
    "tests/periodic_scheduler_fixture.mjs",
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
agent_dir=pathlib.Path(os.environ['PI_CODING_AGENT_DIR']).expanduser() if os.environ.get('PI_CODING_AGENT_DIR') else home/'.config/pi/agent'
extension=agent_dir/'extensions/periodic-checks.ts'
extension.parent.mkdir(parents=True,exist_ok=True)
link(extension,destination/'periodic-checks.ts')
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


def bundle_files():
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name in FILES:
            bundle.add(ROOT / name, arcname=name, recursive=False)
    return base64.b64encode(archive.getvalue()).decode()


def deploy_worker(host=DEFAULT_HOST):
    profile = HOSTS[host]
    key = "REMOTE_AGENT_" + host.upper().replace("-", "_") + "_SSH"
    target = os.environ.get(key, profile["ssh"])
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    request = json.dumps(
        {
            "archive": bundle_files(),
            "config": {
                name: base64.b64encode((CONFIG / name).read_bytes()).decode()
                for name in CONFIG_FILES
            },
            "revision": revision,
            "home": profile["home"],
            "host": host,
        }
    )
    encoded = base64.b64encode(REMOTE_PROGRAM.encode()).decode()
    argv = [
        *profile["wrap"],
        "python3",
        "-c",
        f"import base64;exec(base64.b64decode('{encoded}'))",
    ]
    remote = (
        subprocess.list2cmdline(argv) if profile["windows_shell"] else shlex.join(argv)
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, remote],
        input=request,
        text=True,
        check=True,
    )


def install_periodic_extension():
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    agent_dir = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config/pi/agent"
    )
    extension = agent_dir / "extensions/periodic-checks.ts"
    source = ROOT / "periodic-checks.ts"
    extension.parent.mkdir(parents=True, exist_ok=True)
    if extension.is_symlink():
        if extension.readlink() != source:
            raise FileExistsError(f"Not replacing foreign symlink: {extension}")
    elif extension.exists():
        raise FileExistsError(f"Not replacing unmanaged extension: {extension}")
    else:
        extension.symlink_to(source)


def link_controller():
    install_periodic_extension()
    executable = Path.home() / ".local/bin/remote-agent"
    if executable.is_symlink():
        executable.unlink()
    elif executable.exists():
        raise FileExistsError(f"Not replacing unrelated executable: {executable}")
    executable.symlink_to(ROOT / "remote-agent.py")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=HOSTS, default=DEFAULT_HOST)
    deploy_worker(parser.parse_args().host)
    link_controller()

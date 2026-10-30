#!/usr/bin/env python3
"""Stage and install the separate OpenRouter sandbox; never include keys in images."""

import argparse
import json
import shlex
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = HERE.parent
SSH = ["-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=10"]


def remote(host, args, **kwargs):
    return subprocess.run(["ssh", *SSH, host, shlex.join(args)], check=True, **kwargs)


def key_bytes(path):
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("key file must be owner-only")
    key = path.read_bytes().strip()
    if not key or any(c in b" \t\r\n" for c in key):
        raise ValueError("invalid OpenRouter key")
    return key


def stage(archive_path, pi):
    with tempfile.TemporaryDirectory() as tmp:
        context = Path(tmp)
        settings = json.loads((SANDBOX / "openrouter/settings.json").read_text())
        settings.pop("httpProxy", None)
        models = json.loads((SANDBOX / "openrouter/models.json").read_text())
        models["providers"]["openrouter"]["baseUrl"] = "http://172.30.253.1:3128/api/v1"
        (context / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
        (context / "models.json").write_text(json.dumps(models, indent=2) + "\n")
        with tarfile.open(archive_path, "w:gz") as archive:
            for name in ("host_deploy.py", "session.py", "launch.py", "Dockerfile"):
                archive.add(HERE / name, arcname=name)
            archive.add(SANDBOX / "openrouter_proxy.py", arcname="openrouter_proxy.py")
            archive.add(context / "models.json", arcname="models.json")
            archive.add(context / "settings.json", arcname="settings.json")
            for name in (
                "pi",
                "package.json",
                "README.md",
                "CHANGELOG.md",
                "photon_rs_bg.wasm",
                "fs-worker.cjs",
                "theme",
                "assets",
                "export-html",
                "node_modules",
            ):
                archive.add(pi / name, arcname="pi/" + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="agent-server")
    parser.add_argument(
        "--pi-install", type=Path, default=Path.home() / ".local/share/pi"
    )
    parser.add_argument("--local-project", type=Path, required=True)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    local = str(args.local_project.resolve(strict=True))
    key = key_bytes(args.key_file) if args.key_file else None
    with tempfile.TemporaryDirectory(prefix="openrouter-stage-") as tmp:
        archive = Path(tmp) / "inputs.tar.gz"
        stage(archive, args.pi_install)
        target = remote(
            args.host,
            [
                "python3",
                "-c",
                "import pathlib,tempfile; p=pathlib.Path.home()/'.local/share/openrouter-sandbox'; p.mkdir(parents=True,exist_ok=True,mode=0o700); p.chmod(0o700); print(tempfile.mkdtemp(prefix='stage-',dir=p))",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["scp", *SSH, str(archive), f"{args.host}:{target}/inputs.tar.gz"],
            check=True,
        )
        remote(
            args.host,
            [
                "python3",
                "-c",
                "import tarfile,sys; tarfile.open(sys.argv[1]+'/inputs.tar.gz').extractall(sys.argv[1],filter='data')",
                target,
            ],
        )
        remote(args.host, ["python3", target + "/host_deploy.py", target, local])
        if key is not None:
            script = "import os,pathlib,sys; p=pathlib.Path.home()/'.local/share/openrouter-sandbox/openrouter.key'; f=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); os.fchmod(f,0o600); os.write(f,sys.stdin.buffer.read()); os.close(f)"
            remote(args.host, ["python3", "-c", script], input=key)
        root = remote(
            args.host,
            [
                "python3",
                "-c",
                "import pathlib; print(pathlib.Path.home()/'.local/share/openrouter-sandbox')",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        mapping_path = Path.home() / ".config/remote-agent/remote-projects.json"
        projects = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
        entry = {
            "local": local,
            "remote": "Projects/openrouter-sandbox",
            "pi_executable": root + "/session.py",
            "pi_cancel_executable": root + "/cancel.py",
        }
        if "openrouter-sandbox" in projects and projects["openrouter-sandbox"] != entry:
            raise ValueError("existing controller project mapping differs")
        projects["openrouter-sandbox"] = entry
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        temp = mapping_path.with_suffix(".json.new")
        temp.write_text(json.dumps(projects, indent=2) + "\n")
        temp.chmod(0o600)
        temp.replace(mapping_path)
        remote(
            args.host,
            ["python3", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1])", target],
        )


if __name__ == "__main__":
    main()

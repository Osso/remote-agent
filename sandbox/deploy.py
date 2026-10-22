#!/usr/bin/env python3
"""Stage audited inputs, deploy isolation, then provision one private key."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import stat
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
CLANK_REVISION = "b4f8f6d6308ef5217637f34a24cde016b8b38afc"
SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ConnectTimeout=10",
]


def remote(host, arguments, **options):
    return subprocess.run(
        ["ssh", *SSH_OPTIONS, host, shlex.join(arguments)], check=True, **options
    )


def stage_inputs(destination, clank, pi):
    revision = subprocess.check_output(
        ["git", "-C", str(clank), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != CLANK_REVISION:
        raise ValueError(
            f"clank revision is {revision}; audit expected {CLANK_REVISION}"
        )
    if subprocess.check_output(["git", "-C", str(clank), "status", "--porcelain"]):
        raise ValueError("reviewed clank checkout is not clean")
    manifest = {
        "clank_revision": revision,
        "setup_revision": subprocess.check_output(
            ["git", "-C", str(HERE.parent), "rev-parse", "HEAD"], text=True
        ).strip(),
        "pi_sha256": hashlib.sha256((pi / "pi").read_bytes()).hexdigest(),
    }
    with tarfile.open(destination, "w:gz") as archive:
        for name in (
            "host_deploy.py",
            "firewall.py",
            "session.py",
            "Dockerfile",
            "launch.py",
            "settings.json",
            "models.json",
        ):
            archive.add(HERE / name, arcname=name)
        # Explicit installed-program allowlist: never archive ~/.config/pi or HOME.
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
        source = subprocess.check_output(
            ["git", "-C", str(clank), "archive", "--format=tar", revision]
        )
        with tarfile.open(fileobj=io.BytesIO(source)) as repository:
            for member in repository:
                member.name = "clank/" + member.name
                archive.addfile(
                    member, repository.extractfile(member) if member.isfile() else None
                )
        data = json.dumps(manifest, indent=2).encode()
        member = tarfile.TarInfo("source-manifest.json")
        member.size = len(data)
        member.mode = 0o600
        archive.addfile(member, io.BytesIO(data))
    return manifest


def provision_key(host, key_file):
    if stat.S_IMODE(key_file.stat().st_mode) & 0o077:
        raise ValueError("key file must be owner-only")
    key = key_file.read_bytes().strip()
    if not key or any(character in b" \t\r\n" for character in key):
        raise ValueError("OpenRouter key must be nonempty and contain no whitespace")
    program = (
        "import os, pathlib, sys; "
        "p=pathlib.Path.home()/'.local/share/clank-sandbox/openrouter.key'; "
        "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); "
        "os.fchmod(fd,0o600); "
        "f=os.fdopen(fd,'wb'); f.write(sys.stdin.buffer.read()); f.close()"
    )
    remote(host, ["python3", "-c", program], input=key)


def update_controller_map():
    path = Path.home() / ".config/remote-agent/remote-projects.json"
    projects = json.loads(path.read_text())
    root = "/home/osso/.local/share/clank-sandbox"
    entry = {
        "local": str(Path.home() / "Repos/clank-together"),
        "remote": "Projects/clank-sandbox",
        "pi_executable": root + "/session.py",
        "pi_cancel_executable": root + "/cancel.py",
    }
    if "clank" in projects and projects["clank"] != entry:
        raise ValueError(
            "existing controller clank mapping differs; refusing to overwrite"
        )
    projects["clank"] = entry
    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(projects, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="agent-server")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument(
        "--wrappers-only",
        action="store_true",
        help="update host session routing without rebuilding the image",
    )
    parser.add_argument(
        "--clank-source", type=Path, default=Path.home() / "Repos/clank-together"
    )
    parser.add_argument(
        "--pi-install", type=Path, default=Path.home() / ".local/share/pi"
    )
    args = parser.parse_args()
    if args.wrappers_only:
        target = remote(
            args.host,
            [
                "python3",
                "-c",
                "import pathlib; print(pathlib.Path.home()/'.local/share/clank-sandbox/session.py')",
            ],
            text=True,
            capture_output=True,
        ).stdout.strip()
        subprocess.run(
            [
                "scp",
                *SSH_OPTIONS,
                str(HERE / "session.py"),
                f"{args.host}:{target}.new",
            ],
            check=True,
        )
        remote(
            args.host,
            [
                "python3",
                "-c",
                "import pathlib,sys; p=pathlib.Path(sys.argv[1]); n=p.with_name(p.name+'.new'); n.chmod(0o700); n.replace(p)",
                target,
            ],
        )
        print(
            "Host session wrapper updated; image and credentials unchanged.", flush=True
        )
        return
    if args.key_file is None:
        parser.error("--key-file is required for full deployment")
    # Validate secret ownership without reading it into logs or build context.
    if stat.S_IMODE(args.key_file.stat().st_mode) & 0o077:
        raise ValueError("key file must be owner-only")
    with tempfile.TemporaryDirectory(prefix="clank-stage-") as temporary:
        archive = Path(temporary) / "inputs.tar.gz"
        manifest = stage_inputs(archive, args.clank_source, args.pi_install)
        print(json.dumps(manifest), flush=True)
        stage = remote(
            args.host,
            [
                "python3",
                "-c",
                "import pathlib,tempfile; p=pathlib.Path.home()/'.local/share/clank-sandbox'; p.mkdir(parents=True,exist_ok=True,mode=0o700); p.chmod(0o700); print(tempfile.mkdtemp(prefix='stage-',dir=p))",
            ],
            text=True,
            capture_output=True,
        ).stdout.strip()
        subprocess.run(
            ["scp", *SSH_OPTIONS, str(archive), f"{args.host}:{stage}/inputs.tar.gz"],
            check=True,
        )
        remote(
            args.host,
            [
                "python3",
                "-c",
                "import tarfile,sys; p=sys.argv[1]; tarfile.open(p+'/inputs.tar.gz').extractall(p,filter='data')",
                stage,
            ],
        )
        remote(args.host, ["python3", stage + "/host_deploy.py", stage])
        provision_key(args.host, args.key_file)
        update_controller_map()
        remote(
            args.host,
            ["python3", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1])", stage],
        )
    print(
        "Sandbox installed; key provisioned privately. Live acceptance pending.",
        flush=True,
    )


if __name__ == "__main__":
    main()

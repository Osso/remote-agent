#!/usr/bin/env python3
"""Stage the fixed OpenRouter VM provisioner on the selected SSH host."""

import argparse
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SSH = ["-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ConnectTimeout=10"]
FILES = (
    "host_deploy.py",
    "install_runtime.py",
    "share.py",
    "vm.py",
    "relay.py",
    "repo_proxy.py",
    "session.py",
    "guest_session.py",
    "arch-boxes.asc",
)


def remote(host, args, **kwargs):
    return subprocess.run(["ssh", *SSH, host, shlex.join(args)], check=True, **kwargs)


def stage(path):
    with tarfile.open(path, "w:gz") as archive:
        for name in FILES:
            archive.add(HERE / name, arcname=name)
        archive.add(
            HERE.parent / "openrouter-runtime/tools-packages.txt",
            arcname="tools-packages.txt",
        )


def validate_image(value):
    import re

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError(
            "Pi image must be an immutable sha256 image ID"
        )
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="agent-server")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pi-image", type=validate_image)
    mode.add_argument("--launcher-only", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="openrouter-vm-") as temporary:
        archive = Path(temporary) / "inputs.tar.gz"
        stage(archive)
        target = remote(
            args.host,
            [
                "python3",
                "-c",
                "import tempfile; print(tempfile.mkdtemp(prefix='openrouter-vm-stage-'))",
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        try:
            subprocess.run(
                ["scp", *SSH, str(archive), f"{args.host}:{target}/inputs.tar.gz"],
                check=True,
            )
            remote(
                args.host,
                [
                    "python3",
                    "-c",
                    "import sys,tarfile; tarfile.open(sys.argv[1]+'/inputs.tar.gz').extractall(sys.argv[1],filter='data')",
                    target,
                ],
            )
            command = ["python3", target + "/host_deploy.py", target]
            if args.pi_image:
                command.extend(["--pi-image", args.pi_image])
            if args.launcher_only:
                command.append("--launcher-only")
            remote(args.host, command)
        finally:
            remote(
                args.host,
                [
                    "python3",
                    "-c",
                    "import shutil,sys; shutil.rmtree(sys.argv[1])",
                    target,
                ],
            )


if __name__ == "__main__":
    main()

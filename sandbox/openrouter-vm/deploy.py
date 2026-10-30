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
FILES = ("host_deploy.py", "vm.py", "relay.py", "repo_proxy.py")


def remote(host, args, **kwargs):
    return subprocess.run(["ssh", *SSH, host, shlex.join(args)], check=True, **kwargs)


def stage(path):
    with tarfile.open(path, "w:gz") as archive:
        for name in FILES:
            archive.add(HERE / name, arcname=name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="agent-server")
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
            remote(args.host, ["python3", target + "/host_deploy.py", target])
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

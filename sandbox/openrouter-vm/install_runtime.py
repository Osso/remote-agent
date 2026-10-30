#!/usr/bin/env python3
"""Install a pinned host image's Pi runtime into the provisioned guest."""

import re
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

PI_DEPENDENCIES = ("ca-certificates", "git", "python", "ripgrep", "icu", "curl")
ARCH = Path("/usr/local/bin/arch")


def validate_image(image):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Pi image must be an immutable sha256 image ID")


def ssh_command(config, command):
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + config["known_hosts"],
        "-o",
        "ConnectTimeout=10",
        "-i",
        config["private_key"],
        "-p",
        str(config["ssh_port"]),
        config["admin_user"] + "@" + config["host"],
        shlex.join(command),
    ]


def wait_ready(config, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ssh_command(config, ["cloud-init", "status", "--wait"]),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(15, max(0.1, deadline - time.monotonic())),
            )
            if result.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        "guest root SSH or cloud-init did not become ready within the deadline"
    )


def transfer(config, archive, destination):
    with archive.open("rb") as source:
        subprocess.run(
            ssh_command(config, ["tar", "-xf", "-", "-C", destination]),
            stdin=source,
            check=True,
        )


def install_runtime(stage, config, image):
    validate_image(image)
    packages = (stage / "tools-packages.txt").read_text().split()
    if not packages or any(not re.fullmatch(r"[a-zA-Z0-9@._+-]+", p) for p in packages):
        raise ValueError("invalid or empty tools package list")
    if not ARCH.is_file():
        raise ValueError(f"missing registered host executable: {ARCH}")
    wait_ready(config)
    with tempfile.TemporaryDirectory(prefix="openrouter-pi-") as temporary:
        directory = Path(temporary)
        container = subprocess.run(
            ["docker", "create", "--pull=never", "--network=none", image],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise RuntimeError("docker create returned an invalid container ID")
        try:
            archive = directory / "pi.tar"
            with archive.open("wb") as output:
                subprocess.run(
                    ["docker", "cp", container + ":/opt/pi", "-"],
                    stdout=output,
                    check=True,
                )
            transfer(config, archive, "/opt")
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL
            )
        archive = directory / "runtime.tar"
        with tarfile.open(archive, "w") as output:
            for path, name in (
                (ARCH, "usr/local/bin/arch"),
                (
                    stage / "guest_session.py",
                    "usr/local/lib/openrouter-vm/guest_session.py",
                ),
            ):
                info = output.gettarinfo(str(path), arcname=name)
                info.mode = 0o755
                with path.open("rb") as source:
                    output.addfile(info, source)
        transfer(config, archive, "/")
        subprocess.run(
            ssh_command(
                config, ["/usr/local/bin/arch", "install", *packages, *PI_DEPENDENCIES]
            ),
            check=True,
        )

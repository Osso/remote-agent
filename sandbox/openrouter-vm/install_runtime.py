#!/usr/bin/env python3
"""Install a pinned host image's Pi runtime into the provisioned guest."""

import random
import re
import shlex
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
    last_error = "no readiness attempt completed"
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ssh_command(config, ["cloud-init", "status", "--wait"]),
                capture_output=True,
                text=True,
                timeout=min(15, max(0.1, deadline - time.monotonic())),
            )
            if result.returncode == 0:
                return
            last_error = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"exit {result.returncode}"
            )
        except subprocess.TimeoutExpired as error:
            detail = error.stderr or error.stdout
            if isinstance(detail, bytes):
                detail = detail.decode(errors="replace")
            if detail:
                last_error = f"SSH/cloud-init timed out: {detail.strip()}"
            elif last_error == "no readiness attempt completed":
                last_error = str(error)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        f"guest root SSH or cloud-init did not become ready within the deadline: {last_error}"
    )


def transfer(config, archive, destination):
    with archive.open("rb") as source:
        subprocess.run(
            ssh_command(config, ["tar", "-xf", "-", "-C", destination]),
            stdin=source,
            check=True,
        )


def install_packages(config, packages):
    command = ssh_command(config, ["/usr/local/bin/arch", "install", *packages])
    for attempt in range(1, 4):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end="", file=sys.stdout, flush=True)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr, flush=True)
        if result.returncode == 0:
            return
        print(
            f"Package install attempt {attempt}/3 failed (exit {result.returncode})",
            file=sys.stderr,
            flush=True,
        )
        output = result.stdout + "\n" + result.stderr
        permanent = re.search(
            r"signature|dependency|dependencies|unauthori[sz]ed|forbidden|authentication|invalid or corrupted|permission denied",
            output,
            re.I,
        )
        failures = [
            line
            for line in output.splitlines()
            if re.search(r"failed retrieving file", line, re.I)
        ]
        other_errors = [
            line
            for line in output.splitlines()
            if re.search(r"\berror:", line, re.I)
            and not re.search(
                r"failed retrieving file|failed to commit transaction \(download library error\)",
                line,
                re.I,
            )
        ]
        transient = re.compile(
            r"operation too slow|timed? out|timeout|(?:error|status|response|http)[^\n]*\b(?:429|503)\b|\b(?:429|503)\b",
            re.I,
        )
        if (
            attempt == 3
            or permanent
            or other_errors
            or not failures
            or not all(transient.search(line) for line in failures)
        ):
            raise subprocess.CalledProcessError(
                result.returncode, command, result.stdout, result.stderr
            )
        delay = 2 ** (attempt - 1) + random.uniform(0, 1)
        retry_after = re.search(r"(?im)^\s*retry-after\s*:\s*(.+?)\s*$", output)
        if retry_after:
            value = retry_after.group(1)
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = (
                        parsedate_to_datetime(value) - datetime.now(timezone.utc)
                    ).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            delay = max(delay, seconds)
        print(f"Retrying package download in {delay:.1f}s", file=sys.stderr, flush=True)
        time.sleep(delay)


def install_launcher(stage, config):
    launcher = stage / "guest_session.py"
    if not launcher.is_file():
        raise ValueError(f"missing staged file: {launcher}")
    wait_ready(config)
    with tempfile.TemporaryDirectory(prefix="openrouter-launcher-") as temporary:
        archive = Path(temporary) / "launcher.tar"
        with tarfile.open(archive, "w") as output:
            info = output.gettarinfo(
                str(launcher), arcname="usr/local/lib/openrouter-vm/guest_session.py"
            )
            info.mode = 0o755
            with launcher.open("rb") as source:
                output.addfile(info, source)
        transfer(config, archive, "/")


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
        install_packages(config, [*packages, *PI_DEPENDENCIES])

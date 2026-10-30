#!/usr/bin/env python3
"""Provision or update the fixed VM from a staged directory, as the host user."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path.home() / ".local/share/openrouter-vm"
UNIT = Path.home() / ".config/systemd/user/wowdecomp-vm.service"
IMAGE = "Arch-Linux-x86_64-cloudimg-20261015.611680.qcow2"
URL = "https://geo.mirror.pkgbuild.com/images/latest/" + IMAGE
SHA256 = "08754e28d91b1680086c6211ef4df13559e766fff7cdb4006489b14b5d281f08"
SCRIPTS = ("vm.py", "relay.py", "repo_proxy.py", "session.py", "guest_session.py")
KEYRING = Path(__file__).with_name("arch-boxes.asc")


def run(*args):
    subprocess.run(args, check=True)


def config(root):
    return {
        "image": str(root / "disk.qcow2"),
        "base_image": IMAGE,
        "base_sha256": SHA256,
        "private_key": str(root / "id_ed25519"),
        "known_hosts": str(root / "known_hosts"),
        "ssh_port": 22230,
        "user": "agent",
        "admin_user": "root",
        "host": "127.0.0.1",
    }


def cloud_config(public_key, host_private, host_public):
    return {
        "disable_root": False,
        "ssh_pwauth": False,
        "ssh_deletekeys": False,
        "ssh_genkeytypes": ["ed25519"],
        "bootcmd": [
            [
                "systemctl",
                "mask",
                "--now",
                "systemd-time-wait-sync.service",
                "systemd-timesyncd.service",
            ]
        ],
        "ssh_keys": {"ed25519_private": host_private, "ed25519_public": host_public},
        "users": [
            {"name": "root", "ssh_authorized_keys": [public_key]},
            {
                "name": "agent",
                "uid": 1000,
                "homedir": "/home/agent",
                "shell": "/bin/bash",
                "lock_passwd": True,
                "ssh_authorized_keys": [public_key],
            },
        ],
        "write_files": [
            {
                "path": "/etc/pacman.d/mirrorlist",
                "content": "Server = http://10.0.2.100:3129/$repo/os/$arch\n",
            },
            {
                "path": "/etc/sysctl.d/90-openrouter-ipv6.conf",
                "content": "net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\n",
            },
            {
                "path": "/etc/ssh/sshd_config.d/10-openrouter.conf",
                "content": "PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n",
            },
        ],
        "runcmd": [
            ["mkdir", "-p", "/workspace"],
            ["chown", "1000:1000", "/workspace"],
            ["sysctl", "--system"],
            ["systemctl", "enable", "--now", "sshd"],
        ],
    }


def unit_text(root):
    # JSON double-quoted strings are accepted by systemd's command parser.
    executable = json.dumps(str(root / "vm.py"))
    return (
        "[Unit]\nDescription=OpenRouter writable VM\n\n[Service]\n"
        f"ExecStart=/usr/bin/python3 {executable}\nRestart=on-failure\n"
        "UMask=0077\n\n[Install]\nWantedBy=default.target\n"
    )


def download(url, path):
    with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def verify_image(image, signature):
    with image.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != SHA256:
        raise ValueError("cloud image SHA256 mismatch")
    with tempfile.TemporaryDirectory(prefix="openrouter-image-keyring-") as temporary:
        keyring = str(Path(temporary) / "archlinux.gpg")
        run("gpg", "--batch", "--dearmor", "--output", keyring, str(KEYRING))
        run("gpgv", "--keyring", keyring, str(signature), str(image))


def refresh_seed(root):
    public = (root / "id_ed25519.pub").read_text().strip()
    host_private = (root / "ssh_host_ed25519_key").read_text()
    host_public = (root / "ssh_host_ed25519_key.pub").read_text().strip()
    data = (
        "#cloud-config\n"
        + json.dumps(cloud_config(public, host_private, host_public))
        + "\n"
    )
    user_data = root / "user-data"
    if (
        user_data.is_file()
        and user_data.read_text() == data
        and (root / "seed.iso").is_file()
    ):
        return
    with tempfile.TemporaryDirectory(prefix="seed-", dir=root) as temporary:
        directory = Path(temporary)
        staged_data = directory / "user-data"
        staged_data.write_text(data)
        meta = directory / "meta-data"
        meta.write_text("instance-id: wowdecomp-vm\nlocal-hostname: wowdecomp-vm\n")
        seed = directory / "seed.iso"
        run(
            "xorriso",
            "-as",
            "mkisofs",
            "-output",
            str(seed),
            "-volid",
            "CIDATA",
            "-joliet",
            "-rock",
            str(staged_data),
            str(meta),
        )
        os.replace(seed, root / "seed.iso")
        os.replace(meta, root / "meta-data")
        os.replace(staged_data, user_data)


def install(stage, root=ROOT, unit=UNIT):
    for name in (*SCRIPTS,):
        if not (stage / name).is_file():
            raise ValueError(f"missing staged file: {name}")
    for name in (
        "qemu-system-x86_64",
        "qemu-img",
        "xorriso",
        "ssh-keygen",
        "gpgv",
        "gpg",
        "systemctl",
    ):
        if not shutil.which(name):
            raise ValueError(f"missing host dependency: {name}")
    expected = config(root)
    metadata = root / "vm.json"
    if root.exists():
        if not metadata.is_file() or json.loads(metadata.read_text()) != expected:
            raise ValueError(
                "existing VM state differs or is incomplete; refusing to provision"
            )
        for name in (IMAGE, "disk.qcow2", "seed.iso", "id_ed25519", "known_hosts"):
            if not (root / name).is_file():
                raise ValueError(f"existing VM state missing {name}")
        if not unit.is_file() or unit.read_text() != unit_text(root):
            raise ValueError("existing VM service differs or is missing")
    else:
        if unit.exists():
            raise ValueError("existing VM service without matching state")
        root.mkdir(parents=True, mode=0o700)
        os.chmod(root, 0o700)
        image = root / IMAGE
        signature = root / (IMAGE + ".sig")
        download(URL, image)
        download(URL + ".sig", signature)
        verify_image(image, signature)
        run(
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(image),
            str(root / "disk.qcow2"),
            "64G",
        )
        for key in ("id_ed25519", "ssh_host_ed25519_key"):
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / key))
        host_public = (root / "ssh_host_ed25519_key.pub").read_text().strip()
        (root / "known_hosts").write_text("[127.0.0.1]:22230 " + host_public + "\n")
        refresh_seed(root)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(unit_text(root))
        metadata.write_text(json.dumps(expected, indent=2) + "\n")
        metadata.chmod(0o600)
    refresh_seed(root)
    for name in SCRIPTS:
        shutil.copyfile(stage / name, root / name)
        (root / name).chmod(0o700)
    connection = root / "ssh_config"
    connection.write_text(
        "Host wowdecomp-vm\n  HostName 127.0.0.1\n  Port 22230\n  User agent\n"
        f"  IdentityFile {json.dumps(str(root / 'id_ed25519'))}\n"
        f"  UserKnownHostsFile {json.dumps(str(root / 'known_hosts'))}\n"
        "  StrictHostKeyChecking yes\n  IdentitiesOnly yes\n  ForwardAgent no\n"
    )
    connection.chmod(0o600)
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", "wowdecomp-vm.service")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--pi-image")
    args = parser.parse_args()
    if args.pi_image:
        from install_runtime import validate_image

        validate_image(args.pi_image)
    install(args.stage)
    if args.pi_image:
        from install_runtime import install_runtime

        install_runtime(args.stage, config(ROOT), args.pi_image)

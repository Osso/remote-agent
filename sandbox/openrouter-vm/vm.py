#!/usr/bin/env python3
"""Start the fixed OpenRouter VM under the user service."""

import os
from pathlib import Path

ROOT = Path.home() / ".local/share/openrouter-vm"


def command(root=ROOT):
    network = (
        "user,id=net0,restrict=on,ipv6=off,"
        "hostfwd=tcp:127.0.0.1:22230-:22,"
        f"guestfwd=tcp:10.0.2.100:3128-cmd:python3 {root}/relay.py,"
        f"guestfwd=tcp:10.0.2.100:3129-cmd:python3 {root}/repo_proxy.py"
    )
    return [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-smp",
        "4",
        "-m",
        "8192",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        f"file:{root}/serial.log",
        "-no-reboot",
        "-drive",
        f"file={root}/disk.qcow2,if=virtio,format=qcow2",
        "-drive",
        f"file={root}/seed.iso,media=cdrom,readonly=on",
        "-netdev",
        network,
        "-device",
        "virtio-net-pci,netdev=net0",
    ]


if __name__ == "__main__":
    args = command()
    os.execvp(args[0], args)

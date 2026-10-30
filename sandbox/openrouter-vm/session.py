#!/usr/bin/python3
"""Run the selected Pi invocation over the VM's private SSH connection."""

import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ssh_arguments(config, arguments, tty, environment):
    command = ["env"]
    term = environment.get("TERM", "xterm-256color")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", term):
        raise ValueError("invalid TERM terminal name")
    command.append("TERM=" + term)
    color = environment.get("COLORTERM")
    if color in ("truecolor", "24bit"):
        command.append("COLORTERM=" + color)
    command.extend(["/usr/local/lib/openrouter-vm/guest_session.py", *arguments])
    return [
        "ssh",
        "-tt" if tty else "-T",
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
        "agent@127.0.0.1",
        shlex.join(command),
    ]


def main():
    config = json.loads((ROOT / "vm.json").read_text())
    arguments = ssh_arguments(
        config, sys.argv[1:], sys.stdin.isatty() and sys.stdout.isatty(), os.environ
    )
    os.execvp(arguments[0], arguments)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run another account's worker through the caller's existing authsudo access."""

import argparse
import os
import pwd
import sys
from pathlib import Path


def build_user_environment(account, source):
    home = account.pw_dir
    environment = {
        "HOME": home,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "SHELL": account.pw_shell,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
        "PI_CODING_AGENT_DIR": f"{home}/.config/pi/agent",
    }
    for name, value in source.items():
        if name in ("TERM", "LANG", "LANGUAGE") or name.startswith("LC_"):
            environment[name] = value
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("user")
    parser.add_argument("host")
    parser.add_argument("worker_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        account = pwd.getpwnam(args.user)
        environment = build_user_environment(account, os.environ)
        worker = (
            Path(account.pw_dir) / "Projects/cli/remote-agent/remote-agent-worker.py"
        )
        command = [
            "authsudo",
            "-u",
            account.pw_name,
            "/usr/bin/env",
            "-i",
            *(f"{name}={value}" for name, value in environment.items()),
            "/usr/bin/python3",
            str(worker),
            "--host",
            args.host,
            *args.worker_args,
        ]
        os.execvp(command[0], command)
    except (KeyError, OSError) as error:
        print(
            f"remote-agent: cannot run worker as {args.user}: {error}", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

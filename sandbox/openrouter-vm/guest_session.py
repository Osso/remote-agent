#!/usr/bin/python3
"""Resume Pi in the writable guest without replacing its saved configuration."""

import fcntl
import os
import sys
import uuid
from pathlib import Path

HOME = Path("/home/agent")
WORKSPACE = Path("/workspace")
CREDENTIAL = Path("/etc/openrouter-vm/openrouter.key")
PI = "/opt/pi/pi"


def conversation_id(arguments):
    selected = None
    index = 0
    while index < len(arguments) and arguments[index] != "--":
        flag, separator, value = arguments[index].partition("=")
        if flag in ("--session", "--session-id", "--resume"):
            if not separator:
                index += 1
                value = arguments[index] if index < len(arguments) else ""
            if selected is not None:
                raise ValueError("provide only one conversation ID")
            selected = str(uuid.UUID(value))
        index += 1
    return selected


def lock_conversation(conversation, home):
    directory = home / ".local/state/openrouter-vm"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        directory / (conversation + ".lock"), os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise RuntimeError(f"conversation {conversation} is already running") from None
    os.set_inheritable(descriptor, True)
    return descriptor


def session_environment(inherited, key, home):
    key = key.strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("invalid OpenRouter key")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "USER": "agent",
        "LANG": "C.UTF-8",
        "TERM": inherited.get("TERM", "xterm-256color"),
        "PI_CODING_AGENT_DIR": str(home / ".config/pi/agent"),
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_OFFLINE": "1",
        "PI_KB_DIR": str(home / "kb"),
        "OPENROUTER_API_KEY": key,
    }
    if inherited.get("COLORTERM") in ("truecolor", "24bit"):
        environment["COLORTERM"] = inherited["COLORTERM"]
    return environment


def main():
    os.umask(0o077)
    arguments = sys.argv[1:]
    conversation = conversation_id(arguments)
    if conversation is not None:
        lock_conversation(conversation, HOME)
    environment = session_environment(os.environ, CREDENTIAL.read_text(), HOME)
    (HOME / "kb").mkdir(exist_ok=True)
    os.chdir(WORKSPACE)
    options = (
        []
        if "--export-terminal" in arguments
        else [
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
    )
    os.execve(PI, [PI, *options, *arguments], environment)


if __name__ == "__main__":
    main()

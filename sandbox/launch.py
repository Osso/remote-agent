#!/usr/bin/python3
"""Launch container-local Pi with only the dedicated OpenRouter credential."""

import os
from pathlib import Path
import sys

MODEL = "deepseek/deepseek-v4.1-flash-1017"


def pi_environment(inherited: dict[str, str], key: str) -> dict[str, str]:
    key = key.strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("OpenRouter key must be nonempty and contain no whitespace")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent",
        "USER": "agent",
        "LANG": "C.UTF-8",
        "TERM": inherited.get("TERM", "xterm-256color"),
        "PI_CODING_AGENT_DIR": "/home/agent/.config/pi/agent",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_OFFLINE": "1",
        "TD_NO_UPDATE": "1",
        "OPENROUTER_API_KEY": key,
    }
    if inherited.get("COLORTERM") in ("truecolor", "24bit"):
        environment["COLORTERM"] = inherited["COLORTERM"]
    return environment


def pi_arguments(arguments: list[str]) -> list[str]:
    return [
        "/opt/pi/pi",
        "--provider",
        "openrouter",
        "--model",
        MODEL,
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        *arguments,
    ]


def main() -> None:
    environment = pi_environment(
        os.environ, Path("/run/secrets/openrouter.key").read_text()
    )
    os.umask(0o077)
    arguments = pi_arguments(sys.argv[1:])
    os.execve(arguments[0], arguments, environment)


if __name__ == "__main__":
    main()

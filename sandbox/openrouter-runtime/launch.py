#!/usr/bin/python3
"""Start Pi with the sandbox credential, never on the command line."""

import os
import sys
import tempfile
from pathlib import Path


def environment(inherited, key):
    key = key.strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError("invalid OpenRouter key")
    result = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent",
        "USER": "agent",
        "LANG": "C.UTF-8",
        "TERM": inherited.get("TERM", "xterm-256color"),
        "PI_CODING_AGENT_DIR": "/home/agent/.config/pi/agent",
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_OFFLINE": "1",
        "PI_KB_DIR": "/home/agent/kb",
        "OPENROUTER_API_KEY": key,
    }
    if inherited.get("COLORTERM") in ("truecolor", "24bit"):
        result["COLORTERM"] = inherited["COLORTERM"]
    return result


def install_configuration(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "models.json"):
        data = (source / name).read_bytes()
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(data)
                file.close()
                temporary.replace(destination / name)
            finally:
                temporary.unlink(missing_ok=True)


def main():
    env = environment(os.environ, Path("/run/secrets/openrouter.key").read_text())
    os.umask(0o077)
    install_configuration(Path("/opt/sandbox/config"), Path(env["PI_CODING_AGENT_DIR"]))
    Path(env["PI_KB_DIR"]).mkdir(exist_ok=True)
    os.execve(
        "/opt/pi/pi",
        [
            "/opt/pi/pi",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            *sys.argv[1:],
        ],
        env,
    )


if __name__ == "__main__":
    main()

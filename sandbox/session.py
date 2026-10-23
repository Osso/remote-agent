#!/usr/bin/env python3
"""Run Pi in a conversation-owned container; cancel only that container."""

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

LABEL = "remote-agent.conversation"
PROMPT_DIRECTORY = "/home/agent/.remote-agent-prompts"
PROMPT_LIMIT = 1024 * 1024


def container_name(conversation):
    return "clank-pi-" + hashlib.sha256(conversation.encode()).hexdigest()[:32]


def load_config(path):
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise TypeError("runtime config must be a JSON object")
    for key in (
        "image",
        "network",
        "home_volume",
        "workspace_volume",
        "secret_file",
        "firewall_service",
    ):
        value = config.get(key)
        if not isinstance(value, str) or not value or value.startswith("-"):
            raise ValueError(f"invalid runtime {key}")
    for key in ("network", "home_volume", "workspace_volume", "firewall_service"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config[key]):
            raise ValueError(f"invalid runtime {key}: expected a name")
    secret = Path(config["secret_file"])
    if not secret.is_absolute() or "," in str(secret) or not secret.is_file():
        raise ValueError("invalid runtime secret_file: expected an absolute file path")
    return config


def parse_arguments(arguments):
    config = Path(__file__).resolve().with_name("runtime.json")
    args = list(arguments)
    if args[:1] == ["--config"]:
        if len(args) < 2:
            raise ValueError("--config requires a path")
        config = Path(args[1])
        args = args[2:]
    if args[:1] == ["--cancel"]:
        if len(args) != 2 or not args[1] or args[1].startswith("-"):
            raise ValueError("--cancel requires exactly one conversation ID")
        return config, args, args[1], True
    conversation = None
    i = 0
    while i < len(args) and args[i] != "--":
        flag, separator, value = args[i].partition("=")
        if flag in ("--session-id", "--session", "--resume"):
            if not separator:
                i += 1
                value = args[i] if i < len(args) else ""
            if not value or value.startswith("-") or conversation is not None:
                raise ValueError("provide exactly one --session ID or --resume ID")
            conversation = value
        i += 1
    return config, args, conversation, False


def checked_command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"{arguments[0]} {arguments[1]} {arguments[-1]} failed ({result.returncode}): {detail}"
        )
    return result.stdout


def inspect_owned_container(name, conversation):
    result = subprocess.run(
        ["docker", "inspect", "--type", "container", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if result.returncode == 1 and (
            f"No such object: {name}" in result.stderr
            or f"No such container: {name}" in result.stderr
        ):
            return None
        raise RuntimeError(f"docker inspect failed: {result.stderr.strip()}")
    records = json.loads(result.stdout)
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("docker inspect returned an unexpected container count")
    record = records[0]
    if (record["Config"].get("Labels") or {}).get(LABEL) != conversation:
        raise ValueError(f"refusing container {name}: foreign or missing {LABEL} label")
    return record["State"]["Status"]


def terminal_options(environment):
    options = []
    term = environment.get("TERM", "")
    color = environment.get("COLORTERM", "")
    if term:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", term):
            raise ValueError("invalid TERM terminal name")
        options.extend(["--env", f"TERM={term}"])
    if color:
        if color not in ("truecolor", "24bit"):
            raise ValueError("invalid COLORTERM color capability")
        options.extend(["--env", f"COLORTERM={color}"])
    return options


def container_options(config, network, tty):
    options = [
        "--init",
        "--read-only",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "4g",
        "--cpus",
        "4",
        "--network",
        network,
        "--dns",
        "1.1.1.1",
        "--dns",
        "8.8.8.8",
        "--sysctl",
        "net.ipv6.conf.all.disable_ipv6=1",
        "--mount",
        "type=volume,dst=/tmp",
        "--mount",
        f"type=volume,src={config['home_volume']},dst=/home/agent",
        "--mount",
        f"type=volume,src={config['workspace_volume']},dst=/workspace",
        "--mount",
        f"type=bind,src={config['secret_file']},dst=/run/secrets/openrouter.key,readonly",
        "--workdir",
        "/workspace",
        "--interactive",
    ]
    if tty:
        options.append("--tty")
    return options


def read_prompt(path):
    source = Path(path)
    if not source.is_absolute():
        raise ValueError("prompt source must be an absolute path")
    try:
        # Nonblocking open prevents an accidental FIFO prompt from hanging the wrapper.
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("prompt source must be a regular file")
            data = handle.read(PROMPT_LIMIT + 1)
    except OSError as error:
        raise ValueError(f"cannot read prompt {source}: {error}") from error
    if len(data) > PROMPT_LIMIT:
        raise ValueError("prompt exceeds 1 MiB limit")
    return data


def prepare_prompts(arguments):
    forwarded = []
    prompts = []
    for argument in arguments:
        if not argument.startswith("@"):
            forwarded.append(argument)
            continue
        data = read_prompt(argument[1:])
        target = f"{PROMPT_DIRECTORY}/{uuid.uuid4().hex}.txt"
        prompts.append((target, data))
        forwarded.append("@" + target)
    return forwarded, prompts


def copy_prompts(name, prompts):
    # The image owns the private directory (0700, uid 1000). Root-owned Docker
    # copies must be readable by uid 1000; never change the host source mode.
    with tempfile.TemporaryDirectory(prefix="clank-prompt-") as temporary:
        for target, data in prompts:
            staged = Path(temporary) / Path(target).name
            staged.write_bytes(data)
            staged.chmod(0o644)
            checked_command(["docker", "cp", str(staged), f"{name}:{target}"])


def run_session(config, arguments, conversation):
    forwarded, prompts = prepare_prompts(arguments)
    if conversation is None and prompts:
        raise ValueError("host prompt files require --session ID or --resume ID")
    checked_command(["systemctl", "is-active", "--quiet", config["firewall_service"]])
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    network = config["network"] if conversation is not None else "none"
    options = container_options(config, network, tty) + terminal_options(os.environ)
    if conversation is None:
        return subprocess.run(
            ["docker", "run", "--rm", *options, config["image"], *forwarded],
            check=False,
        ).returncode
    name = container_name(conversation)
    status = inspect_owned_container(name, conversation)
    if status is not None:
        if status != "exited":
            raise ValueError(f"refusing container {name}: conversation is {status}")
        checked_command(["docker", "rm", "--volumes", name])
    checked_command(
        [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            f"{LABEL}={conversation}",
            *options,
            config["image"],
            *forwarded,
        ]
    )
    copy_prompts(name, prompts)
    checked_command(["systemctl", "is-active", "--quiet", config["firewall_service"]])

    def forward_winch(signum, frame):
        try:
            # Docker may still be starting, or may have exited before detach.
            if inspect_owned_container(name, conversation) == "running":
                checked_command(["docker", "kill", "--signal", "WINCH", name])
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            print(
                f"sandbox session: SIGWINCH forwarding to {name}: {error}",
                file=sys.stderr,
            )

    previous = signal.signal(signal.SIGWINCH, forward_winch)
    try:
        return subprocess.run(
            ["docker", "start", "--attach", "--interactive", name], check=False
        ).returncode
    finally:
        signal.signal(signal.SIGWINCH, previous)


def main(arguments=None):
    try:
        path, arguments, conversation, cancel = parse_arguments(
            sys.argv[1:] if arguments is None else arguments
        )
        if cancel:
            name = container_name(conversation)
            if inspect_owned_container(name, conversation) is not None:
                checked_command(["docker", "rm", "--force", "--volumes", name])
            return 0
        return run_session(load_config(path), arguments, conversation)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(f"sandbox session: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

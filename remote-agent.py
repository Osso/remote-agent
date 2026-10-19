#!/usr/bin/env python3
"""Laptop controller for explicitly requested remote terminal sessions."""

import argparse
import base64
import json
import os
import shlex
import random
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
CONFIG = Path.home() / ".config/remote-agent"
PROJECTS_FILE = CONFIG / "remote-projects.json"
HOSTS = json.loads((CONFIG / "remote-hosts.json").read_text())
DEFAULT_HOST = "agent-server"


def ssh_target(host):
    key = "REMOTE_AGENT_" + host.upper().replace("-", "_") + "_SSH"
    return os.environ.get(key, HOSTS[host]["ssh"])


def destination(host=DEFAULT_HOST, user=None):
    profile = HOSTS[host]
    if user is not None:
        if profile["windows_shell"] or profile["wrap"]:
            raise ValueError("--user requires a native Linux host without wrapping")
        return [
            ssh_target(host),
            "python3",
            profile["home"] + "/Projects/cli/remote-agent/remote-agent-user.py",
            user,
            host,
        ]
    argv = [
        ssh_target(host),
        *profile["wrap"],
        "python3",
        profile["home"] + "/Projects/cli/remote-agent/remote-agent-worker.py",
    ]
    if host != "desktop":
        argv += ["--host", host]
    return argv


def validate_project(project, projects=None):
    projects = (
        projects if projects is not None else json.loads(PROJECTS_FILE.read_text())
    )
    if project not in projects:
        raise ValueError("project must be one of: " + ", ".join(projects))


def terminal_colorterm():
    return (
        "truecolor"
        if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
        else ""
    )


def request_with_terminal_colors(request):
    if request["action"] == "start":
        return {**request, "colorterm": terminal_colorterm()}
    return request


def query(host, request, user=None):
    result = run_read_only(
        ["ssh", *KEEPALIVE, *destination(host, user)],
        input=json.dumps(request_with_terminal_colors(request)),
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise ValueError(
            f"{host}{' (user ' + user + ')' if user else ''}: "
            f"worker failed (exit {result.returncode}): "
            + result.stdout.strip()
            + result.stderr.strip()
        )
    output = result.stdout
    if HOSTS[host]["windows_shell"]:
        output = output.removesuffix(WINDOWS_TITLE.decode())
    return json.loads(output)


def list_sessions(host=DEFAULT_HOST, user=None):
    sessions, errors = [], []
    try:
        context = {"host": host}
        if user is not None:
            context["user"] = user
        sessions = [
            {**session, **context}
            for session in query(host, {"action": "list"}, user)["sessions"]
        ]
    except (ValueError, OSError) as error:
        errors.append(str(error))
    print(json.dumps({"sessions": sessions, "errors": errors}))
    return int(bool(errors))


def validate_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise argparse.ArgumentTypeError(
            "name must be 1–64 letters/digits/_/-, starting with a letter/digit"
        )
    return value


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("start")
    start.add_argument("project", type=validate_name)
    start.add_argument("name", type=validate_name, nargs="?")
    start.add_argument(
        "--agent-harness",
        choices=("claude", "pi"),
        default="pi",
        help="agent harness (default: pi)",
    )
    start.add_argument("--prompt-file", type=Path)
    start.add_argument("--resume", metavar="ID", help="conversation id to continue")
    projects = commands.add_parser("projects")
    listing = commands.add_parser("list")
    listing.add_argument(
        "--user",
        type=validate_name,
        help="inspect another Unix user's sessions via authsudo",
    )
    account = commands.add_parser(
        "claude-account", help="list or switch Claude accounts on the selected host"
    )
    account_mode = account.add_mutually_exclusive_group()
    account_mode.add_argument("use", nargs="?", type=validate_name, metavar="PROFILE")
    account_mode.add_argument("--name-current", type=validate_name, metavar="PROFILE")
    account_mode.add_argument("--login", type=validate_name, metavar="PROFILE")
    pi_login = commands.add_parser(
        "pi-login", help="log in a Pi provider on the selected host"
    )
    pi_login.add_argument("provider", type=validate_name)
    paste_parser = commands.add_parser(
        "paste", help="send the laptop clipboard image to Ctrl+V"
    )
    sync = commands.add_parser("sync", help="copy working files (not .git)")
    sync.add_argument("project")
    sync.add_argument("--pull", action="store_true", help="selected host to laptop")
    sync.add_argument("--dry-run", action="store_true")
    fetch = commands.add_parser(
        "fetch",
        help="copy remote files/folders into ~/Downloads/remote-agent",
    )
    fetch.add_argument(
        "paths",
        nargs="+",
        help="\\\\wsl$ path as printed, /abs, ~/rel, or relative to a project",
    )
    fetch.add_argument(
        "--project", help="project of relative paths (default: the one that has it)"
    )
    fetch.add_argument(
        "--open", action="store_true", help="open images as Chromium tabs"
    )
    for command in (
        start,
        projects,
        listing,
        account,
        pi_login,
        paste_parser,
        sync,
        fetch,
    ):
        command.add_argument("--host", choices=HOSTS, default=DEFAULT_HOST)
    for action in ("status", "logs", "attach", "cancel"):
        command = commands.add_parser(action)
        command.add_argument("name", type=validate_name)
        command.add_argument("--host", choices=HOSTS, default=DEFAULT_HOST)
        if action == "attach":
            command.add_argument(
                "--user",
                type=validate_name,
                help="attach another Unix user's session via authsudo",
            )
    return parser.parse_args()


# The desktop's Windows OpenSSH appends a terminal-title escape to stdout.
WINDOWS_TITLE = b"\x1b]2;Terminal\x07"
LIST_UNCOMMITTED = ["ls-files", "-z", "--modified", "--others", "--exclude-standard"]


def parse_file_list(output, windows_shell=True):
    """NUL-terminated git paths; only the known Windows trailer may follow."""
    *paths, trailer = output.split(b"\0")
    if trailer not in ((b"", WINDOWS_TITLE) if windows_shell else (b"",)):
        raise ValueError(f"unexpected trailing output from git: {trailer!r}")
    return b"".join(path + b"\0" for path in paths)


def sync(args):
    """Git fast-forwards commits, then rsync copies the files git reports as
    uncommitted (so git's global excludes apply too)."""
    projects = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    validate_project(args.project, projects)
    project = projects[args.project]
    host = args.host
    profile = HOSTS[host]
    target, wrap = ssh_target(host), profile["wrap"]
    local = project["local"].rstrip("/")
    remote = f"{profile['home']}/{project['remote'].rstrip('/')}"
    branch = subprocess.run(
        ["git", "-C", local, "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not branch:
        raise ValueError(f"laptop checkout has no current branch: {local}")
    # ext:: passes arguments unquoted; ssh:// quotes the path with '', which
    # the desktop's Windows shell does not strip. updateInstead fast-forwards
    # the desktop's checked-out branch and refuses if its tree is dirty.
    url = " ".join(
        [
            "ext::ssh",
            target,
            *wrap,
            "git",
            "-c",
            "receive.denyCurrentBranch=updateInstead",
            "%s",
            remote,
        ]
    )
    if not profile["windows_shell"]:
        url = f"ssh://{target}{remote}"
    git = ["git", "-C", local, "-c", "protocol.ext.allow=always"]
    dry_run = ["--dry-run"] if args.dry_run else []
    if args.pull:
        verb = ["fetch", *dry_run] if args.dry_run else ["pull", "--ff-only"]
        git_command = [*git, *verb, url, branch]
        remote_list = ["git", "-C", remote, *LIST_UNCOMMITTED]
        list_command = (
            ["ssh", target, *wrap, *remote_list]
            if profile["windows_shell"]
            else ["ssh", target, shlex.join(remote_list)]
        )
    else:
        git_command = [*git, "push", *dry_run, url, f"{branch}:{branch}"]
        list_command = ["git", "-C", local, *LIST_UNCOMMITTED]
    if not profile["windows_shell"] and not args.pull:
        git_command += [
            "--receive-pack=git -c receive.denyCurrentBranch=updateInstead receive-pack"
        ]
    if code := subprocess.call(git_command):
        return code
    files = parse_file_list(
        subprocess.run(
            list_command, stdin=subprocess.DEVNULL, capture_output=True, check=True
        ).stdout,
        profile["windows_shell"],
    )
    if not files:
        print("No uncommitted files to copy.")
        return 0
    remote_path = f"{target}:{remote}/"
    source, destination = (
        (remote_path, local + "/") if args.pull else (local + "/", remote_path)
    )
    # --ignore-missing-args: deleted tracked files are listed but not copied;
    # rsync never deletes on the receiving side.
    rsync = [
        "rsync",
        "-a",
        "--itemize-changes",
        "--from0",
        "--files-from=-",
        "--ignore-missing-args",
        "--rsync-path=" + " ".join([*wrap, "rsync"]),
        *dry_run,
        source,
        destination,
    ]
    return subprocess.run(rsync, input=files).returncode


def send(request, host=DEFAULT_HOST):
    return subprocess.run(
        ["ssh", *destination(host)],
        input=json.dumps(request_with_terminal_colors(request)),
        text=True,
    ).returncode


def claude_account(args):
    host = args.host
    request = {"action": "claude-account"}
    if args.use:
        return send({**request, "use": args.use}, host)
    if args.name_current:
        return send({**request, "name_current": args.name_current}, host)
    if not args.login:
        return send(request, host)
    if code := send({**request, "release": True}, host):
        return code
    if code := subprocess.call(["ssh", "-t", *destination(host), "--claude-login"]):
        print(
            "remote-agent: login did not finish; switch back with "
            "`remote-agent claude-account PROFILE`",
            file=sys.stderr,
        )
        return code
    return send({**request, "name_current": args.login}, host)


MACOS_CLIPBOARD_SCRIPT = """
ObjC.import('AppKit');
function run() {
    const image = $.NSImage.alloc.initWithPasteboard($.NSPasteboard.generalPasteboard);
    if (image.isNil()) throw new Error('clipboard has no image');
    const bitmap = $.NSBitmapImageRep.imageRepWithData(image.TIFFRepresentation);
    if (bitmap.isNil()) throw new Error('clipboard image cannot be decoded');
    const png = bitmap.representationUsingTypeProperties(
        $.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary);
    if (png.isNil()) throw new Error('clipboard image cannot be converted to PNG');
    return ObjC.unwrap(png.base64EncodedStringWithOptions(0));
}
"""


def read_clipboard_png():
    if sys.platform == "darwin":
        image = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", MACOS_CLIPBOARD_SCRIPT],
            capture_output=True,
        )
        if image.returncode:
            detail = image.stderr.decode(errors="replace").strip()
            raise ValueError(f"macOS clipboard read failed: {detail}")
        try:
            png = base64.b64decode(image.stdout.strip(), validate=True)
        except ValueError as error:
            raise ValueError("macOS clipboard returned invalid image data") from error
    else:
        image = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "image/png"], capture_output=True
        )
        png = image.stdout if image.returncode == 0 else b""
    if not png:
        raise ValueError("laptop clipboard has no PNG image")
    return png


def paste(host=DEFAULT_HOST):
    png = read_clipboard_png()
    request = {"action": "clipboard", "png": base64.b64encode(png).decode()}
    return send(request, host)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


UNC_PREFIXES = ("\\\\wsl$\\", "\\\\wsl.localhost\\")
SSH_FAILED = 255
FETCH_ATTEMPTS = 4


def run_read_only(argv, **kwargs):
    """Run a read-only desktop command, retrying when ssh itself fails (exit 255,
    e.g. a connection timeout) with jittered exponential backoff."""
    for attempt in range(FETCH_ATTEMPTS):
        result = subprocess.run(argv, **kwargs)
        if result.returncode != SSH_FAILED or attempt == FETCH_ATTEMPTS - 1:
            return result
        delay = 2**attempt * (0.5 + random.random())
        print(
            f"remote-agent: {('desktop' if 'desktop' in argv else 'remote host')} connection failed; retrying in {delay:.1f}s",
            file=sys.stderr,
        )
        time.sleep(delay)


# Detect a dead link (interface/IP change, sleep) in ~15 s instead of hanging.
KEEPALIVE = [
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=3",
    "-o",
    "ConnectTimeout=10",
]
RECONNECT_MAX_DELAY = 30
# Undo modes tmux/Claude enable that a dropped connection never turns off:
# mouse tracking, focus events, bracketed paste, kitty keyboard, modifyOtherKeys,
# hidden cursor, alternate screen.
TERMINAL_RESET = (
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l"
    "\x1b[?1004l\x1b[?2004l\x1b[<99u\x1b[>4;0m\x1b[?25h\x1b[?1049l"
)


def restore_terminal():
    if not sys.stdout.isatty():
        return
    sys.stdout.write(TERMINAL_RESET)
    sys.stdout.flush()
    subprocess.run(["stty", "sane"], check=True)


def attach(name, host=DEFAULT_HOST, user=None):
    """Attach until a clean detach; reconnect after any dropped connection.
    The worker restarts (with resume) a session lost to a desktop restart."""
    if user is not None:
        # Authorization/configuration failures must not enter the reconnect loop.
        query(host, {"action": "list"}, user)
    failures = 0
    while True:
        code = subprocess.call(
            [
                "mosh",
                "--no-init",
                "--",
                *destination(host, user),
                f"--colorterm={terminal_colorterm()}",
                "--attach",
                name,
            ]
        )
        restore_terminal()
        if code == 0:
            # Mosh does not propagate the worker's refused-attach exit status.
            # Verify the session survived rather than silently accepting refusal.
            query(host, {"action": "status", "name": name}, user)
            return 0
        delay = min(2**failures, RECONNECT_MAX_DELAY) * (0.5 + random.random() / 2)
        failures += 1
        print(
            f"remote-agent: connection lost (exit {code}); reconnecting in "
            f"{delay:.1f}s, Ctrl-C to stop",
            file=sys.stderr,
        )
        time.sleep(delay)


def project_containing(path, projects, host=DEFAULT_HOST):
    """Find the unique checkout containing a relative path on the selected host."""
    profile = HOSTS[host]
    candidates = {
        name: f"{project['remote'].rstrip('/')}/{path}"
        for name, project in projects.items()
    }
    if profile["windows_shell"]:
        argv = [
            "ssh",
            ssh_target(host),
            *profile["wrap"][:-1],
            "--cd",
            "~",
            profile["wrap"][-1],
            "python3",
            "-",
        ]
    else:
        argv = ["ssh", ssh_target(host), "python3", "-"]
    script = (
        "import json, os\n"
        f"print(json.dumps([n for n, p in {json.dumps(candidates)}.items()"
        " if os.path.exists(p)]))\n"
    )
    result = run_read_only(argv, input=script, stdout=subprocess.PIPE, text=True)
    if result.returncode:
        raise ValueError(
            f"could not check projects on {host} (exit {result.returncode}); pass --project NAME"
        )
    output = result.stdout
    if profile["windows_shell"]:
        output = output.removesuffix(WINDOWS_TITLE.decode())
    found = json.loads(output)
    if len(found) != 1:
        where = f"in {', '.join(found)}" if found else "in no project"
        raise ValueError(f"{path} exists {where}; pass --project NAME")
    return found[0]


def desktop_wsl_distribution():
    profile = HOSTS.get("desktop")
    if profile is None:
        raise ValueError("UNC paths require a configured desktop host profile")
    wrap = profile["wrap"]
    try:
        return wrap[wrap.index("-d") + 1]
    except (ValueError, IndexError) as error:
        raise ValueError(
            "desktop host profile must specify a WSL distribution with -d"
        ) from error


def fetch_target(path, project, projects, host=DEFAULT_HOST):
    if project:
        validate_project(project, projects)
    for prefix in UNC_PREFIXES:
        if path.lower().startswith(prefix.lower()):
            if host != "desktop":
                raise ValueError("UNC paths require desktop; pass --host desktop")
            configured_distro = desktop_wsl_distribution()
            distro, _, relative = path[len(prefix) :].partition("\\")
            if distro.lower() != configured_distro.lower():
                raise ValueError(f"unknown WSL distribution: {distro}")
            return host, "/" + relative.replace("\\", "/")
    if path.startswith(("/", "~/")):
        absolute = (
            path if path.startswith("/") else HOSTS[host]["home"] + "/" + path[2:]
        )
        return host, absolute
    project = project or project_containing(path, projects, host)
    return (
        host,
        f"{HOSTS[host]['home']}/{projects[project]['remote'].rstrip('/')}/{path}",
    )


def fetch(args):
    """Copy desktop files or folders to the laptop; optionally open images.

    rsync runs from the desktop home (`--cd ~`) so home paths go relative, and
    -s sends names over rsync's protocol instead of the desktop's Windows shell,
    which would split them on spaces. Everything lands in ~/Downloads/remote-agent
    (folders keep their name and sync incrementally). --open shows named images,
    images that changed inside fetched folders, or every image of a folder that
    was already up to date."""
    projects = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    targets = [
        fetch_target(path, args.project, projects, args.host) for path in args.paths
    ]
    downloads = Path.home() / "Downloads/remote-agent"
    downloads.mkdir(parents=True, exist_ok=True)
    to_open = []
    for host, remote in targets:
        remote = remote.rstrip("/")
        profile = HOSTS[host]
        home, wrap = profile["home"], profile["wrap"]
        rsync_path = (
            " ".join([*wrap[:-1], "--cd", "~", wrap[-1], "rsync"])
            if profile["windows_shell"]
            else "rsync"
        )
        local = downloads / Path(remote).name
        source = remote[len(home) + 1 :] if remote.startswith(home + "/") else remote
        result = run_read_only(
            [
                "rsync",
                "-a",
                "-s",
                "--out-format=%n",
                f"--rsync-path={rsync_path}",
                f"{ssh_target(host)}:{source}",
                f"{local.parent}/",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        if result.returncode:
            return result.returncode
        print(local)
        if local.is_file():
            changed = [local]
        else:
            changed = [local.parent / name for name in result.stdout.splitlines()]
        images = [
            f for f in changed if f.suffix.lower() in IMAGE_SUFFIXES and f.is_file()
        ]
        if not images and local.is_dir():
            images = sorted(
                f
                for f in local.rglob("*")
                if f.suffix.lower() in IMAGE_SUFFIXES and f.is_file()
            )
        to_open += images
    if args.open and to_open:
        subprocess.Popen(
            ["chromium", *(f"file://{f}" for f in to_open)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return 0


def main():
    args = parse_args()
    try:
        if args.action == "fetch":
            return fetch(args)
        if args.action == "sync":
            return sync(args)
        if args.action == "claude-account":
            return claude_account(args)
        if args.action == "pi-login":
            return subprocess.call(
                ["ssh", "-t", *destination(args.host), "--pi-login", args.provider]
            )
        if args.action == "paste":
            return paste(args.host)
        if args.action == "projects":
            projects = json.loads(PROJECTS_FILE.read_text())
            print(
                json.dumps(
                    {
                        "projects": {
                            name: {
                                key: value
                                for key, value in project.items()
                                if key != "host"
                            }
                            for name, project in projects.items()
                        }
                    }
                )
            )
            return 0
        if args.action == "list":
            return list_sessions(args.host, args.user)
        if args.action == "start":
            validate_project(args.project)
        host = args.host
        if args.action == "attach":
            return attach(args.name, host, args.user)
        request = {"action": args.action}
        if args.action == "start":
            request.update(
                name=args.name or args.project,
                repo=args.project,
                agent=args.agent_harness,
            )
            if args.resume:
                request["resume"] = args.resume
            if args.prompt_file is not None:
                request["prompt"] = args.prompt_file.read_text(encoding="utf-8")
        elif args.action not in ("projects", "list"):
            request["name"] = args.name
        if args.action == "logs":
            print(
                "Warning: terminal logs can include sensitive content.", file=sys.stderr
            )
        return send(request, host)
    except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"remote-agent: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

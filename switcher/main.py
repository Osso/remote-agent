"""Desktop entry point and terminal session-selection loop."""

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .actions import PICKER_TITLE, open_session
from .inventory import (
    Session,
    collect_sessions,
    load_local_sessions,
    load_remote_sessions,
    local_control_db_path,
)
from .mutations import archive_local_session, delete_local_session
from .picker import Pick, choose_session


def _refresh_selection(selected: Session) -> Session:
    if selected.backend == "local":
        sessions = load_local_sessions(local_control_db_path(), socket.gethostname())
    else:
        sessions = load_remote_sessions(selected.host, selected.user)
    matches = [session for session in sessions if session.key == selected.key]
    if len(matches) != 1:
        raise RuntimeError(
            "Session identity changed or disappeared. Refresh and select it again."
        )
    return matches[0]


def _open(selected: Session) -> None:
    open_session(_refresh_selection(selected))


def _confirm_delete(session: Session) -> bool:
    try:
        answer = input(f"Delete '{session.name}' at {session.cwd}? [Y/n] ")
    except EOFError:
        return False
    return answer.strip().casefold() in {"", "y", "yes"}


def _perform(pick: Pick) -> str | None:
    """Archive or delete a refreshed local session; None means delete was declined."""
    session = _refresh_selection(pick.session)
    if session.backend != "local":
        verb = "archived" if pick.action == "archive" else "deleted"
        raise RuntimeError(f"Remote sessions cannot be {verb} from this switcher.")
    if pick.action == "archive":
        archive_local_session(local_control_db_path(), session)
        return f"Archived '{session.name}'."
    if not _confirm_delete(session):
        return None
    session = _refresh_selection(session)
    delete_local_session(local_control_db_path(), session)
    return f"Deleted '{session.name}'."


def _wait_for_close() -> None:
    try:
        input("Press Enter to close.")
    except EOFError:
        return


def _pick() -> int:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "The picker needs a terminal. Run agent-switcher to open it in WezTerm."
        )
    sys.stdout.write(f"\x1b]0;{PICKER_TITLE}\x07")
    sys.stdout.flush()
    last_error: list[str] = []
    notice = ""
    while True:
        print(
            "Loading laptop, agent-server and aso sessions...",
            file=sys.stderr,
            flush=True,
        )
        sessions, errors = collect_sessions()
        try:
            pick = choose_session(sessions, [*errors, *last_error], notice)
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            _wait_for_close()
            return 1
        if pick is None:
            return 0
        try:
            if pick.action == "open":
                _open(pick.session)
                return 0
            notice = _perform(pick) or ""
            last_error = []
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            last_error = [str(error)]
            notice = ""


def _parse_arguments(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select a Pi session on this laptop, agent-server or aso."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list", action="store_true", help="Print read-only inventory as JSON."
    )
    mode.add_argument(
        "--pick", action="store_true", help="Run the selector in the current terminal."
    )
    mode.add_argument(
        "--open", metavar="KEY", help="Open an exact selection key returned by --list."
    )
    return parser.parse_args(arguments)


def _print_inventory() -> int:
    sessions, errors = collect_sessions()
    payload = {
        "sessions": [{**asdict(session), "key": session.key} for session in sessions],
        "errors": errors,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if sessions or not errors else 1


def _open_key(key: str) -> int:
    sessions, errors = collect_sessions()
    selected = next((session for session in sessions if session.key == key), None)
    if selected is None:
        detail = "; ".join(errors) if errors else "refresh with --list"
        raise RuntimeError(f"Unknown or stale session selection: {detail}.")
    _open(selected)
    return 0


def run(arguments: list[str] | None = None) -> int:
    args = _parse_arguments(arguments)
    if args.list:
        return _print_inventory()
    if args.pick:
        return _pick()
    if args.open:
        return _open_key(args.open)
    return _launch_picker()


def _wezterm_runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(base) / "wezterm"


def _live_gui_sockets() -> list[Path] | None:
    """GUI sockets a live WezTerm still listens on, ignoring stale socket files.

    A crashed GUI leaves gui-sock files and a discovery symlink behind; only a
    bound socket appears in /proc/net/unix. None means liveness is unknown.
    """
    prefix = str(_wezterm_runtime_dir()) + "/"
    try:
        table = Path("/proc/net/unix").read_text()
    except OSError:
        return None
    paths = {
        columns[-1]
        for columns in (line.split() for line in table.splitlines()[1:])
        if columns
        and columns[-1].startswith(prefix)
        and Path(columns[-1]).name.startswith("gui-sock-")
    }
    return sorted(Path(path) for path in paths)


def _discovery_socket() -> Path | None:
    display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
    links = sorted(_wezterm_runtime_dir().glob("*-org.wezfurlong.wezterm"))
    preferred = [
        link for link in links if display and link.name.startswith(display + "-")
    ]
    candidates = preferred or links
    return candidates[0].resolve() if candidates else None


def _report_failure(error: str) -> None:
    subprocess.run(
        ["notify-send", "--app-name=agent-switcher", "Agent switcher", error],
        check=False,
    )
    raise RuntimeError(error)


def _run_wezterm(command: list[str], environment: dict[str, str] | None = None) -> int:
    result = subprocess.run(
        command, env=environment, capture_output=True, text=True, check=False
    )
    if result.returncode:
        _report_failure(
            f"Cannot open the picker in WezTerm: {result.stderr.strip() or result.returncode}"
        )
    return 0


def _launch_picker() -> int:
    """Spawn the picker into a live GUI, or start the only GUI when none is live."""
    launcher = str(Path(sys.argv[0]).resolve())
    live = _live_gui_sockets()
    if live is None:
        _report_failure("Cannot read WezTerm socket liveness; no GUI was started.")
    if not live:
        return _run_wezterm(["wezterm", "start", "--", launcher, "--pick"])
    environment = None
    if _discovery_socket() not in live:
        environment = {**os.environ, "WEZTERM_UNIX_SOCKET": str(live[0])}
    return _run_wezterm(
        [
            "wezterm",
            "cli",
            "--no-auto-start",
            "spawn",
            "--new-window",
            "--",
            launcher,
            "--pick",
        ],
        environment,
    )


def main() -> int:
    try:
        return run()
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"agent-switcher: {error}", file=sys.stderr)
        return 1

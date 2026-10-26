"""Desktop entry point and terminal session-selection loop."""

import argparse
import json
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
from .picker import choose_session


def _refresh_selection(selected: Session) -> Session:
    if selected.backend == "local":
        sessions = load_local_sessions(local_control_db_path(), socket.gethostname())
    else:
        sessions = load_remote_sessions(selected.host)
    matches = [session for session in sessions if session.key == selected.key]
    if len(matches) != 1:
        raise RuntimeError(
            "Session identity changed or disappeared. Refresh and select it again."
        )
    return matches[0]


def _open(selected: Session) -> None:
    open_session(_refresh_selection(selected))


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
    while True:
        print(
            "Loading laptop and agent-server sessions...", file=sys.stderr, flush=True
        )
        sessions, errors = collect_sessions()
        try:
            selected = choose_session(sessions, [*errors, *last_error])
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            _wait_for_close()
            return 1
        if selected is None:
            return 0
        try:
            _open(selected)
            return 0
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            last_error = [str(error)]


def _parse_arguments(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select a Pi session on this laptop or agent-server."
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
    launcher = str(Path(sys.argv[0]).resolve())
    result = subprocess.run(["wezterm", "start", "--", launcher, "--pick"], check=False)
    if result.returncode:
        raise RuntimeError(f"WezTerm exited with status {result.returncode}.")
    return 0


def main() -> int:
    try:
        return run()
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"agent-switcher: {error}", file=sys.stderr)
        return 1

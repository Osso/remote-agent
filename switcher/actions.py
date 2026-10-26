"""Select a session's existing WezTerm pane or open it in one new tab."""

import json
import os
import subprocess
from pathlib import Path

from .inventory import Session

WEZTERM_APP = "org.wezfurlong.wezterm"
PICKER_TITLE = "Agent sessions"
DEFAULT_HOST = "agent-server"  # remote-agent's implicit --host.


def _run(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot run {arguments[0]}: {error}") from error
    if result.returncode:
        raise RuntimeError(
            f"{' '.join(arguments[:3])} failed: {result.stderr.strip() or result.returncode}"
        )
    return result.stdout


def _wezterm(*arguments: str) -> str:
    return _run(["wezterm", "cli", "--no-auto-start", *arguments])


def _list_panes() -> list[dict]:
    try:
        panes = json.loads(_wezterm("list", "--format", "json"))
    except ValueError as error:
        raise RuntimeError(f"Invalid WezTerm pane inventory: {error}") from error
    if not isinstance(panes, list) or any(
        not isinstance(pane, dict)
        or type(pane.get("pane_id")) is not int
        or type(pane.get("window_id")) is not int
        for pane in panes
    ):
        raise RuntimeError("Invalid WezTerm pane inventory.")
    return panes


def _list_windows() -> list[dict]:
    try:
        windows = json.loads(_run(["niri", "msg", "--json", "windows"]))
    except ValueError as error:
        raise RuntimeError(f"Invalid Niri window inventory: {error}") from error
    if not isinstance(windows, list) or any(
        not isinstance(window, dict) for window in windows
    ):
        raise RuntimeError("Invalid Niri window inventory.")
    return windows


def _terminal(pid: int) -> str | None:
    try:
        path = os.readlink(f"/proc/{pid}/fd/0")
    except OSError:
        return None
    live = path.startswith("/dev/pts/") and not path.endswith(" (deleted)")
    return path if live else None


def _attach_target(args: list[str]) -> tuple[str, str | None, list[str]]:
    """Host, user and positionals of remote-agent arguments; --host/--user are global."""
    options: dict[str, str | None] = {"--host": DEFAULT_HOST, "--user": None}
    positionals = []
    remaining = iter(args)
    for arg in remaining:
        option, separator, value = arg.partition("=")
        if option in options:
            options[option] = value if separator else next(remaining, "")
        else:
            positionals.append(arg)
    return options["--host"] or "", options["--user"], positionals


def _attach_clients(session: Session) -> list[int]:
    """PIDs of local remote-agent clients attached to this account's session."""
    expected = (session.host, None, ["attach", session.handle])
    clients = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # The kernel names a process after the executed file: the binary, or
            # the script under an interpreter (whose path is then argv[1]).
            if (entry / "comm").read_text().strip() != "remote-agent":
                continue
            args = (entry / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        args = args.split("\0")[:-1]
        program = next(
            (i for i, arg in enumerate(args[:2]) if Path(arg).name == "remote-agent"),
            None,
        )
        if program is not None and _attach_target(args[program + 1 :]) == expected:
            clients.append(int(entry.name))
    return clients


def _session_terminals(session: Session) -> list[str]:
    if session.backend == "remote":
        pids = _attach_clients(session)
    else:
        pids = [session.pid] if session.pid else [0]
    terminals = {_terminal(pid) for pid in pids}
    if None in terminals:
        raise RuntimeError(
            "Session is already open, but its process has no terminal. No duplicate was started."
        )
    return sorted(terminals)


def _niri_window(target_window: int, picker_window: int | None) -> int | None:
    """The only WezTerm Niri window not titled as a picker; None if already focused."""
    if target_window == picker_window:
        return None
    candidates = [
        window
        for window in _list_windows()
        if window.get("app_id") == WEZTERM_APP
        and not (picker_window is not None and window.get("title") == PICKER_TITLE)
    ]
    if len(candidates) != 1 or type(candidates[0].get("id")) is not int:
        raise RuntimeError(
            f"{len(candidates)} WezTerm windows exist; cannot identify which to focus. Nothing was changed."
        )
    return candidates[0]["id"]


def _focus(niri_window: int | None) -> None:
    if niri_window is not None:
        _run(["niri", "msg", "action", "focus-window", "--id", str(niri_window)])


def _spawn_arguments(session: Session) -> list[str]:
    if session.backend == "remote":
        return ["--", "remote-agent", "--host", session.host, "attach", session.handle]
    return ["--cwd", session.cwd, "--", "pi", "--session", session.handle]


def open_session(session: Session) -> None:
    """Activate the session's existing WezTerm pane, or open it in one new tab."""
    if session.backend == "remote" and session.status != "running":
        raise RuntimeError(
            f"Remote session is {session.status}. Resume it explicitly with remote-agent start PROJECT NEW-NAME --resume {session.session_id}; no replacement was created."
        )
    panes = _list_panes()
    own_pane = os.environ.get("WEZTERM_PANE")
    picker_window = next(
        (pane["window_id"] for pane in panes if str(pane["pane_id"]) == own_pane),
        None,
    )
    running = session.backend == "remote" or session.status != "stopped"
    terminals = _session_terminals(session) if running else []
    if len(terminals) > 1:
        raise RuntimeError(
            f"Session attachment is ambiguous: {len(terminals)} terminals ({', '.join(terminals)}). No tab was selected."
        )
    if terminals:
        pane = next((p for p in panes if p.get("tty_name") == terminals[0]), None)
        if pane is None:
            raise RuntimeError(
                f"Session is open outside WezTerm on {terminals[0]}. Switch to it there; no duplicate was started."
            )
        niri_window = _niri_window(pane["window_id"], picker_window)
        _wezterm("activate-pane", "--pane-id", str(pane["pane_id"]))
        _focus(niri_window)
        return
    windows = sorted({pane["window_id"] for pane in panes} - {picker_window})
    if not windows:
        _wezterm("spawn", "--new-window", *_spawn_arguments(session))
        return
    niri_window = _niri_window(windows[0], picker_window)
    _wezterm("spawn", "--window-id", str(windows[0]), *_spawn_arguments(session))
    _focus(niri_window)

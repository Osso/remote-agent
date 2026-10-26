"""Select an existing terminal or use the installed native session commands."""

import json
import subprocess
from pathlib import Path

from .inventory import Session


def _read_process_ancestors(pid: int) -> set[int]:
    ancestors = set()
    while pid > 0 and pid not in ancestors:
        ancestors.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError) as error:
            raise RuntimeError(
                "Session process exited; refresh the session list."
            ) from error
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return ancestors


def _run_terminal(arguments: list[str], cwd: str | None = None) -> None:
    try:
        result = subprocess.run(arguments, cwd=cwd, check=False)
    except OSError as error:
        raise RuntimeError(
            f"Cannot open session with {arguments[0]}: {error}"
        ) from error
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} exited with status {result.returncode}.")


def _query_windows() -> list[dict]:
    try:
        result = subprocess.run(
            ["niri", "msg", "--json", "windows"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        windows = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(
            f"Session is already open; cannot identify its terminal: {error}"
        ) from error
    if result.returncode:
        raise RuntimeError(f"Cannot inspect Niri windows: {result.stderr.strip()}")
    if not isinstance(windows, list) or any(
        not isinstance(window, dict) for window in windows
    ):
        raise RuntimeError("Niri returned an invalid window inventory.")
    return windows


def _matches_window(window: dict, session: Session, ancestors: set[int]) -> bool:
    expected_app = window.get("app_id") == "com.mitchellh.ghostty"
    expected_title = window.get("title") == session.window_title
    pid = window.get("pid")
    expected_process = type(pid) is int and pid in ancestors
    return expected_app and expected_title and expected_process


def _focus_local(session: Session, sessions: list[Session]) -> None:
    same_title = [
        s
        for s in sessions
        if s.backend == "local"
        and s.status != "stopped"
        and s.window_title == session.window_title
    ]
    if len(same_title) != 1 or session.pid is None:
        raise RuntimeError(
            "Session is already open, but its terminal identity is ambiguous. No duplicate was started."
        )
    ancestors = _read_process_ancestors(session.pid)
    matches = [
        window
        for window in _query_windows()
        if _matches_window(window, session, ancestors)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Session is already open in an unidentified or hidden Ghostty tab. Bring that tab forward; no duplicate was started."
        )
    window_id = matches[0].get("id")
    if type(window_id) is not int:
        raise RuntimeError("Niri returned an invalid window identity.")
    _run_terminal(["niri", "msg", "action", "focus-window", "--id", str(window_id)])


def open_session(session: Session, sessions: list[Session]) -> bool:
    """Return True after focusing; terminal commands return False after exiting."""
    if session.backend == "remote":
        if session.status != "running":
            raise RuntimeError(
                f"Remote session is {session.status}. Resume it explicitly with remote-agent start PROJECT NEW-NAME --resume {session.session_id}; no replacement was created."
            )
        _run_terminal(
            ["remote-agent", "--host", session.host, "attach", session.handle]
        )
        return False
    if session.status == "stopped":
        _run_terminal(["pi", "--session", session.handle], cwd=session.cwd)
        return False
    _focus_local(session, sessions)
    return True

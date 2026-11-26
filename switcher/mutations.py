"""Rename, stop, archive and delete local Pi sessions."""

import os
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .inventory import Session

# Pi removes these tables' rows when a transcript is deleted. Only tables that
# exist are touched, so a partial or newer Pi schema cannot abort the cleanup.
_CONTROL_TABLES = (
    "session_sandbox_profiles",
    "session_metadata",
    "multi_agent_agents",
    "multi_agent_runtime_owners",
    "multi_agent_terminal_outbox",
    "multi_agent_mailbox_messages",
    "multi_agent_counters_v2",
)
_DELETE_DESCENDANTS_SQL = """
WITH RECURSIVE descendants(session_path) AS (
    SELECT session_path FROM session_metadata
    WHERE is_subagent = 1 AND parent_session_path IN (?, ?)
    UNION
    SELECT child.session_path
    FROM descendants AS parent JOIN session_metadata AS child
    ON child.parent_session_path IN (
        CASE WHEN substr(parent.session_path, -4) = '.zst'
            THEN substr(parent.session_path, 1, length(parent.session_path) - 4)
            ELSE parent.session_path END,
        CASE WHEN substr(parent.session_path, -4) = '.zst'
            THEN parent.session_path ELSE parent.session_path || '.zst' END
    )
    WHERE child.is_subagent = 1
)
SELECT session_path FROM descendants WHERE session_path NOT IN (?, ?)
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _open_control_db(control_db: Path) -> sqlite3.Connection:
    if not control_db.exists():
        raise RuntimeError(f"Pi control database does not exist: {control_db}")
    db = sqlite3.connect(
        control_db.absolute().as_uri() + "?mode=rw", uri=True, timeout=5
    )
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def _require_stopped_local(session: Session, verb: str) -> None:
    _require_local(session, verb)
    if session.status != "stopped":
        raise RuntimeError(
            f"Session is {session.status}; stop it in Pi before you {verb} it."
        )


def _require_local(session: Session, verb: str) -> None:
    if session.backend != "local":
        raise RuntimeError(f"Only local sessions can be {verb} from this switcher.")


def rename_local_session(control_db: Path, session: Session, name: str) -> None:
    """Set a local session's display name the way Pi's /name does."""
    _require_local(session, "renamed")
    name = name.strip()
    if not name:
        raise RuntimeError("Session name must not be empty.")
    with closing(_open_control_db(control_db)) as db:
        changed = db.execute(
            "UPDATE session_metadata"
            " SET name = ?, name_revision = name_revision + 1, updated_at = ?"
            " WHERE session_path = ? AND id = ?",
            (name, _now(), session.handle, session.session_id),
        ).rowcount
        if changed != 1:
            raise RuntimeError("Session metadata changed; refresh and retry.")
        db.commit()


def stop_local_session(session: Session, timeout: float = 10) -> None:
    """Send SIGTERM to the session's bound Pi process and wait for it to exit."""
    _require_local(session, "stopped")
    if session.status != "running" or session.pid is None:
        raise RuntimeError(
            f"Session is {session.status}; only running sessions can be stopped."
        )
    os.kill(session.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(session.pid):
            return
        time.sleep(0.1)
    raise RuntimeError(f"Pi process {session.pid} did not exit within {timeout:g}s.")


def _process_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in {"Z", "X"}


def archive_local_session(control_db: Path, session: Session) -> None:
    """Hide a stopped local session as a Pi archive without moving its transcript."""
    _require_stopped_local(session, "archive")
    transcript = Path(session.handle)
    if not transcript.is_file():
        raise RuntimeError(f"Session transcript does not exist: {transcript}")
    now = _now()
    with closing(_open_control_db(control_db)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute(
                "UPDATE session_metadata SET archived_at = ?, updated_at = ?"
                " WHERE session_path = ? AND archived_at IS NULL",
                (now, now, session.handle),
            ).rowcount
            if changed != 1:
                raise RuntimeError(
                    "Session metadata changed or is already archived; refresh and retry."
                )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def delete_local_session(control_db: Path, session: Session) -> None:
    """Remove a stopped local session's transcripts and control records."""
    _require_stopped_local(session, "delete")
    with closing(_open_control_db(control_db)) as db:
        descendants = [
            row[0]
            for row in db.execute(
                _DELETE_DESCENDANTS_SQL,
                (
                    session.handle,
                    session.handle + ".zst",
                    session.handle,
                    session.handle + ".zst",
                ),
            )
        ]
    paths = [session.handle, *descendants]
    deleted, failure = _remove_transcripts(paths)
    with closing(_open_control_db(control_db)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            tables = [
                name
                for (name,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
                if name in _CONTROL_TABLES
            ]
            for path in deleted:
                for table in tables:
                    db.execute(f"DELETE FROM {table} WHERE session_path = ?", (path,))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    if failure is not None:
        raise RuntimeError(failure)


def _remove_transcripts(paths: list[str]) -> tuple[list[str], str | None]:
    """Trash or unlink existing transcripts; missing ones are already deleted."""
    deleted = [path for path in paths if not Path(path).exists()]
    existing = [path for path in paths if Path(path).exists()]
    if existing:
        try:
            subprocess.run(
                ["trash", "--", *existing],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    failure = None
    for path in existing:
        if not Path(path).exists():
            deleted.append(path)
            continue
        try:
            Path(path).unlink()
            deleted.append(path)
        except OSError as error:
            failure = failure or f"Cannot delete {path}: {error}"
    return deleted, failure

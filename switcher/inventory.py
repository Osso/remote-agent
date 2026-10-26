"""Read-only Pi metadata and registered remote-agent session inventory."""

import base64
import json
import os
import random
import socket
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_REMOTE_TIMEOUT = 10
_FRESH_SECONDS = 5 * 60  # Pi SESSION_CHECK_STALE_MS.


@dataclass(frozen=True)
class Session:
    host: str
    session_id: str
    name: str
    cwd: str
    status: Literal["running", "stopped", "unknown"]
    backend: Literal["local", "remote"]
    handle: str
    window_title: str | None = None
    pid: int | None = None

    @property
    def key(self) -> str:
        encoded = json.dumps(
            [self.host, self.backend, self.handle, self.session_id, self.cwd],
            ensure_ascii=True,
        )
        return base64.urlsafe_b64encode(encoded.encode()).decode().rstrip("=")


class InventoryError(ValueError):
    """Source failure, optionally retaining valid entries from a partial response."""

    def __init__(self, message: str, sessions: list[Session] | None = None):
        super().__init__(message)
        self.sessions = sessions if sessions is not None else []


def _process_stat(pid: int | None) -> tuple[str, int | None] | None:
    if pid is None or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except PermissionError:
        return ("?", None)
    end = raw.rfind(")")
    fields = raw[end + 2 :].split()
    if end < 0 or len(fields) < 20:
        raise InventoryError(f"Invalid process stat for PID {pid}")
    if fields[0] in {"Z", "X"}:
        return None
    return fields[0], int(fields[19])


def _fresh(timestamp: str | None, now: datetime) -> bool:
    if not timestamp:
        return False
    try:
        stamp = datetime.fromisoformat(timestamp)
        age = (now - stamp).total_seconds()
    except (ValueError, TypeError):
        return False
    return 0 <= age <= _FRESH_SECONDS


def _identity_matches(listener: sqlite3.Row, ticks: int | None) -> bool:
    try:
        identity = json.loads(listener["runtime_instance_id"] or "null")
    except (ValueError, TypeError):
        return False
    return (
        isinstance(identity, dict)
        and ticks is not None
        and type(identity.get("pid")) is int
        and identity["pid"] == listener["pid"]
        and type(identity.get("startTimeTicks")) is int
        and identity["startTimeTicks"] == ticks
    )


def _local_status(
    row: sqlite3.Row,
    health: sqlite3.Row | None,
    listener: sqlite3.Row | None,
    now: datetime,
) -> tuple[Literal["running", "stopped", "unknown"], int | None]:
    health_stat = _process_stat(health["pid"]) if health else None
    listener_stat = _process_stat(listener["pid"]) if listener else None
    if health_stat is None and listener_stat is None:
        return "stopped", None
    if listener is None or listener_stat is None:
        return "unknown", None
    if listener["session_path"] != row["session_path"]:
        return "unknown", None
    pid = listener["pid"]
    bound = (
        _identity_matches(listener, listener_stat[1])
        and listener["session_path_asserted_at"] == listener["updated_at"]
        and _fresh(listener["updated_at"], now)
    )
    healthy = (
        health is not None
        and health["pid"] == pid
        and health["check_status"] == "ok"
        and health["agent_generation"] > 0
        and health["checked_generation"] == health["agent_generation"]
        and _fresh(health["last_checked_at"], now)
    )
    return ("running" if bound and healthy else "unknown"), pid


def load_local_sessions(control_db: Path, host: str) -> list[Session]:
    """Read current Pi tables without opening transcripts or reconciling state."""
    if not control_db.exists():
        return []
    with closing(
        sqlite3.connect(
            control_db.absolute().as_uri() + "?mode=ro", uri=True, timeout=2
        )
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        metadata = db.execute(
            """SELECT session_path, id, cwd, name, first_message
               FROM session_metadata
               WHERE is_subagent = 0 AND archived_at IS NULL"""
        ).fetchall()
        health = {
            row["session_id"]: row for row in db.execute("SELECT * FROM session_health")
        }
        listeners = {
            row["recipient_session_id"]: row
            for row in db.execute(
                "SELECT * FROM runtime_mailbox_listeners WHERE recipient_agent_id_key = ''"
            )
        }
    sessions = []
    now = datetime.now(UTC)
    for row in metadata:
        path_parts = row["session_path"].replace("\\", "/").split("/")
        if row["id"] in {"architect", "supervisor"} or {
            "architect-sessions",
            "supervisor-sessions",
        }.intersection(path_parts):
            continue
        cwd = row["cwd"]
        raw_name = row["name"]
        message = row["first_message"].strip().splitlines()
        name = (raw_name or "").strip() or (message[0] if message else cwd)
        title = (
            f"π - {raw_name} - {Path(cwd).name}"
            if raw_name
            else f"π - {Path(cwd).name}"
        )
        status, pid = _local_status(
            row, health.get(row["id"]), listeners.get(row["id"]), now
        )
        sessions.append(
            Session(
                host,
                row["id"],
                name,
                cwd,
                status,
                "local",
                row["session_path"],
                title,
                pid,
            )
        )
    return sessions


def _parse_remote(output: str, host: str) -> list[Session]:
    try:
        payload = json.loads(output)
    except ValueError as error:
        raise InventoryError(f"Invalid remote-agent JSON: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
        raise InventoryError("Invalid remote-agent response: expected sessions array")
    errors = payload.get("errors", [])
    if not isinstance(errors, list) or any(
        not isinstance(error, str) for error in errors
    ):
        raise InventoryError(
            "Invalid remote-agent response: expected errors array of strings"
        )
    sessions = []
    for row in payload["sessions"]:
        if not isinstance(row, dict):
            raise InventoryError("Invalid remote-agent session: expected object")
        for field in ("name", "project", "cwd", "conversation"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise InventoryError(
                    f"Invalid remote-agent session: expected nonempty {field}"
                )
        if any(
            type(row.get(field)) is not bool for field in ("running", "server_running")
        ):
            raise InventoryError(
                "Invalid remote-agent session: expected running/server_running booleans"
            )
        status: Literal["running", "stopped", "unknown"] = "unknown"
        if row["running"] and row["server_running"]:
            status = "running"
        elif not row["running"] and not row["server_running"]:
            status = "stopped"
        sessions.append(
            Session(
                host,
                row["conversation"],
                row["name"],
                row["cwd"],
                status,
                "remote",
                row["name"],
            )
        )
    if errors:
        raise InventoryError("; ".join(errors), sessions)
    return sessions


def _transient(message: str) -> bool:
    text = message.lower()
    if any(
        word in text
        for word in ("permission denied", "authentication", "host key verification")
    ):
        return False
    return any(
        word in text
        for word in (
            "connection refused",
            "connection reset",
            "connection timed out",
            "timed out",
            "temporary failure in name resolution",
            "network is unreachable",
            "no route to host",
        )
    )


def load_remote_sessions(host: str = "agent-server") -> list[Session]:
    """Query only the registered installed executable and its requested host/account."""
    if host.lower() in {
        "localhost",
        "127.0.0.1",
        "::1",
        socket.gethostname().lower(),
    }:
        raise InventoryError("Local session inventory must not use SSH")
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["remote-agent", "--host", host, "list"],
                timeout=_REMOTE_TIMEOUT,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            failure = InventoryError(
                f"remote-agent {host} timed out after {_REMOTE_TIMEOUT}s"
            )
            if attempt == 2:
                raise failure from error
        else:
            if result.returncode:
                detail = (
                    result.stderr.strip() or result.stdout.strip() or "no error output"
                )
                failure = InventoryError(
                    f"remote-agent {host} exited {result.returncode}: {detail}"
                )
                if result.stdout.lstrip().startswith("{"):
                    try:
                        _parse_remote(result.stdout, host)
                    except InventoryError as error:
                        failure = InventoryError(f"{failure}: {error}", error.sessions)
            else:
                try:
                    return _parse_remote(result.stdout, host)
                except InventoryError as error:
                    failure = error
            if attempt == 2 or not _transient(str(failure)):
                raise failure
        time.sleep(0.25 * (2**attempt) + random.uniform(0, 0.1))
    raise AssertionError("Retry loop exhausted")


def local_control_db_path() -> Path:
    """Resolve Pi's current control database without creating its state directory."""
    state = os.environ.get("PI_CODING_AGENT_STATE_DIR")
    state_dir = (
        Path(state)
        if state
        else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        / "pi"
    )
    return state_dir / "control.sqlite"


def collect_sessions() -> tuple[list[Session], list[str]]:
    """Merge independent sources; return source errors for visible presentation."""
    sessions: list[Session] = []
    errors: list[str] = []
    try:
        sessions.extend(
            load_local_sessions(local_control_db_path(), socket.gethostname())
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        errors.append(f"Local inventory: {error}")
    try:
        sessions.extend(load_remote_sessions())
    except InventoryError as error:
        sessions.extend(error.sessions)
        errors.append(f"Remote inventory (agent-server): {error}")
    except OSError as error:
        errors.append(f"Remote inventory (agent-server): {error}")
    return sorted(
        sessions,
        key=lambda session: (
            session.backend != "remote",
            session.name.casefold(),
            session.host,
            session.key,
        ),
    ), errors

"""Full-terminal session selection using the installed fzf executable."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import TYPE_CHECKING
import unicodedata

if TYPE_CHECKING:
    from .inventory import Session


def _safe(value: str) -> str:
    """Replace terminal controls, including Unicode format and line separators."""
    return "".join(char if char.isprintable() else " " for char in value)


def _width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in ("W", "F")
        else 1
        for char in value
    )


def _shorten_name(value: str) -> str:
    maximum_cells = 48
    if _width(value) <= maximum_cells:
        return value
    cells = 0
    for index, char in enumerate(value):
        cells += _width(char)
        if cells >= maximum_cells:
            return value[:index] + "…"
    return value


def choose_session(sessions: list[Session], errors: list[str]) -> Session | None:
    """Return the selected original session, or None for cancellation/empty inventory.

    Inventory failures without sessions, fzf failures, and invalid selections raise
    RuntimeError. Display data never supplies commands or session identity.
    """
    safe_errors = [_safe(error) for error in errors]
    if not sessions:
        if safe_errors:
            raise RuntimeError("Cannot select a session: " + "; ".join(safe_errors))
        print("No sessions found.", file=sys.stderr)
        return None

    columns = [("NAME", "HOST", "STATUS", "PATH")]
    columns.extend(
        (_shorten_name(_safe(s.name)), _safe(s.host), _safe(s.status), _safe(s.cwd))
        for s in sessions
    )
    widths = [max(_width(row[i]) for row in columns) for i in range(3)]
    display = [
        "  ".join(
            value + " " * (widths[i] - _width(value)) if i < 3 else value
            for i, value in enumerate(row)
        )
        for row in columns
    ]
    rows = {}
    for session, label in zip(sessions, display[1:]):
        selector = hashlib.sha256(repr(session.key).encode("utf-8")).hexdigest()
        rows[selector + "\t" + label] = session

    header = "Enter select / Esc cancel"
    if safe_errors:
        header += "\nInventory errors: " + "; ".join(safe_errors)
    header += "\n" + display[0]
    # Only terminal/locale settings and executable lookup cross the child boundary.
    # In particular, no FZF_* options, commands, or user shell configuration apply.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE")
        if key in os.environ
    }
    try:
        result = subprocess.run(
            [
                "fzf",
                "--no-height",
                "--no-multi",
                "--no-preview",
                "--delimiter=\t",
                "--with-nth=2..",
                "--header",
                header,
                "--prompt=Session> ",
            ],
            input="\n".join(rows) + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
        )
    except OSError as error:
        raise RuntimeError(f"Cannot launch fzf: {_safe(str(error))}") from error
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise RuntimeError(
            f"fzf exited with status {result.returncode}: {_safe(result.stderr).strip()}"
        )
    selected = result.stdout.removesuffix("\n")
    if selected not in rows:
        raise RuntimeError("fzf returned an empty, stale, or unrecognized selection")
    return rows[selected]

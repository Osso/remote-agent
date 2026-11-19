"""Archive and delete mutations against an isolated Pi control database."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from switcher.inventory import Session
from switcher.mutations import archive_local_session, delete_local_session


class MutationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="switcher-mutations-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.db = self.home / "control.sqlite"
        self.transcript = self.home / "sessions" / "parent.jsonl"
        self.transcript.parent.mkdir()
        self.transcript.write_text("parent transcript\n")
        self.child_transcript = self.transcript.with_name("child.jsonl")
        self.child_transcript.write_text("child transcript\n")
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.trash_record = self.home / "trash.json"
        self.write_trash()
        self.environment = patch.dict(
            os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with sqlite3.connect(self.db) as db:
            db.executescript("""
                CREATE TABLE session_metadata (
                    session_path TEXT PRIMARY KEY, id TEXT, cwd TEXT, name TEXT,
                    parent_session_path TEXT, archived_at TEXT, is_subagent INTEGER,
                    message_count INTEGER, first_message TEXT, updated_at TEXT);
                CREATE TABLE multi_agent_agents (
                    session_path TEXT, agent_id TEXT, updated_at TEXT);
                """)
            db.executemany(
                "INSERT INTO session_metadata (session_path, id, name, "
                "parent_session_path, is_subagent) VALUES (?, ?, ?, ?, ?)",
                [
                    (str(self.transcript), "parent", "Parent", None, 0),
                    (
                        str(self.child_transcript),
                        "child",
                        "Child",
                        str(self.transcript),
                        1,
                    ),
                ],
            )
            db.execute(
                "INSERT INTO multi_agent_agents VALUES (?, 'agent_1', 'now')",
                (str(self.transcript),),
            )

    def write_trash(self):
        self.trash = self.bin / "trash"
        self.trash.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "paths = sys.argv[2:]\n"
            f"pathlib.Path({str(self.trash_record)!r}).write_text(json.dumps(paths))\n"
            "for path in paths:\n"
            "    pathlib.Path(path).unlink()\n"
        )
        self.trash.chmod(0o755)

    def session(self, status="stopped", handle=None, backend="local"):
        return Session(
            host="aso",
            session_id="parent",
            name="Parent",
            cwd=str(self.home),
            status=status,
            backend=backend,
            handle=str(handle or self.transcript),
        )

    def metadata(self, path=None):
        with sqlite3.connect(self.db) as db:
            return db.execute(
                "SELECT archived_at FROM session_metadata WHERE session_path = ?",
                (str(path or self.transcript),),
            ).fetchone()

    def test_archive_marks_metadata_and_keeps_transcript_in_place(self):
        archive_local_session(self.db, self.session())
        (archived_at,) = self.metadata()
        self.assertIsNotNone(archived_at)
        self.assertTrue(self.transcript.is_file())
        self.assertEqual(self.transcript.read_text(), "parent transcript\n")
        self.assertFalse(self.trash_record.exists())

    def test_archive_refuses_live_sessions_without_changing_metadata(self):
        for status, backend in (("running", "local"), ("unknown", "local")):
            with self.subTest(status=status):
                with self.assertRaisesRegex(RuntimeError, "stop it in Pi"):
                    archive_local_session(self.db, self.session(status=status))
                self.assertIsNone(self.metadata()[0])
        with self.assertRaisesRegex(RuntimeError, "Only local sessions"):
            archive_local_session(self.db, self.session(backend="remote"))

    def test_archive_refuses_a_missing_transcript(self):
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            archive_local_session(
                self.db, self.session(handle=self.home / "absent.jsonl")
            )
        self.assertIsNone(self.metadata()[0])

    def test_delete_trashes_the_tree_and_removes_control_rows(self):
        delete_local_session(self.db, self.session())
        self.assertFalse(self.transcript.exists())
        self.assertFalse(self.child_transcript.exists())
        self.assertEqual(
            json.loads(self.trash_record.read_text()),
            [str(self.transcript), str(self.child_transcript)],
        )
        self.assertIsNone(self.metadata())
        self.assertIsNone(self.metadata(self.child_transcript))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM multi_agent_agents").fetchone()[0], 0
            )

    def test_delete_removes_a_stale_row_whose_transcript_is_missing(self):
        self.transcript.unlink()
        delete_local_session(self.db, self.session())
        self.assertIsNone(self.metadata())

    def test_delete_refuses_live_sessions_before_removing_anything(self):
        with self.assertRaisesRegex(RuntimeError, "stop it in Pi"):
            delete_local_session(self.db, self.session(status="running"))
        self.assertTrue(self.transcript.is_file())
        self.assertIsNotNone(self.metadata())

    def test_delete_keeps_control_rows_when_a_transcript_cannot_be_removed(self):
        self.trash.unlink()
        self.transcript.unlink()
        self.transcript.mkdir()
        with self.assertRaisesRegex(RuntimeError, "Cannot delete"):
            delete_local_session(self.db, self.session())
        self.assertIsNotNone(self.metadata())
        self.assertIsNone(self.metadata(self.child_transcript))


if __name__ == "__main__":
    unittest.main()

"""Read-only switcher inventory, using SQLite and real disposable processes."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from switcher import inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "control.sqlite"
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE session_metadata (
                session_path TEXT PRIMARY KEY, id TEXT NOT NULL, cwd TEXT NOT NULL,
                name TEXT, parent_session_path TEXT, archived_at TEXT,
                is_subagent INTEGER NOT NULL DEFAULT 0, first_message TEXT NOT NULL
            );
            CREATE TABLE session_health (
                session_id TEXT PRIMARY KEY, agent_generation INTEGER NOT NULL,
                pid INTEGER, last_active_at TEXT, last_checked_at TEXT,
                check_status TEXT NOT NULL, checked_generation INTEGER,
                check_latency_ms INTEGER, updated_at TEXT NOT NULL
            );
            CREATE TABLE runtime_mailbox_listeners (
                recipient_session_id TEXT NOT NULL, recipient_agent_id_key TEXT NOT NULL,
                pid INTEGER NOT NULL, runtime_instance_id TEXT, session_path TEXT,
                session_path_asserted_at TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY (recipient_session_id, recipient_agent_id_key)
            );
        """)
        self.now = datetime.now(UTC).isoformat()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.requests = self.root / "requests.jsonl"
        self.payload = self.root / "payload.json"
        self.payload.write_text('{"sessions": []}')
        executable = self.bin / "remote-agent"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "root = Path(os.environ['INVENTORY_FIXTURE'])\n"
            "with (root / 'requests.jsonl').open('a') as out:\n"
            " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "mode = os.environ.get('INVENTORY_MODE', '')\n"
            "attempt = len((root / 'requests.jsonl').read_text().splitlines())\n"
            "if mode == 'timeout': time.sleep(1)\n"
            "if mode == 'transient' and attempt < 3:\n"
            " print('ssh: Connection refused', file=sys.stderr); sys.exit(1)\n"
            "if mode == 'permanent':\n"
            " print('Permission denied (publickey)', file=sys.stderr); sys.exit(1)\n"
            "print((root / 'payload.json').read_text())\n"
        )
        executable.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "INVENTORY_FIXTURE": str(self.root),
            "PI_CODING_AGENT_STATE_DIR": str(self.root),
        }
        self.env.pop("INVENTORY_MODE", None)
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def metadata(self, session_id="conversation", path=None, **fields):
        path = path or str(self.root / "project" / f"{session_id}.jsonl")
        values = {
            "session_path": path,
            "id": session_id,
            "cwd": "/projects/example",
            "name": "Example",
            "parent_session_path": None,
            "archived_at": None,
            "is_subagent": 0,
            "first_message": "First user request",
            **fields,
        }
        columns = ",".join(values)
        self.conn.execute(
            f"INSERT INTO session_metadata ({columns}) VALUES ({','.join('?' for _ in values)})",
            list(values.values()),
        )
        self.conn.commit()
        return path

    def process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.reap, child)
        return child

    @staticmethod
    def reap(child):
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)

    def bind(self, path, pid, session_id="conversation", **overrides):
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        fields = {
            "recipient_session_id": session_id,
            "recipient_agent_id_key": "",
            "pid": pid,
            "runtime_instance_id": json.dumps(
                {"pid": pid, "startTimeTicks": int(stat[19])}
            ),
            "session_path": path,
            "session_path_asserted_at": self.now,
            "updated_at": self.now,
            **overrides,
        }
        self.conn.execute(
            f"INSERT INTO runtime_mailbox_listeners ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            list(fields.values()),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO session_health VALUES (?, 7, ?, ?, ?, 'ok', 7, 0, ?)",
            (session_id, pid, self.now, self.now, self.now),
        )
        self.conn.commit()

    def local(self):
        return inventory.load_local_sessions(self.db, "workstation")

    def remote_row(self, **updates):
        return {
            "name": "managed-example",
            "project": "example",
            "cwd": "/projects/remote",
            "conversation": "conversation",
            "running": True,
            "server_running": True,
            **updates,
        }

    def remote_payload(self, rows, errors=None):
        data = {"sessions": rows}
        if errors is not None:
            data["errors"] = errors
        self.payload.write_text(json.dumps(data))

    def test_session_is_frozen_and_keys_preserve_host_backend_and_handle(self):
        session = inventory.Session(
            "host", "same", "Name", "/cwd", "stopped", "local", "/a"
        )
        with self.assertRaises(FrozenInstanceError):
            session.name = "changed"
        variants = [
            session,
            inventory.Session("host", "same", "Name", "/cwd", "stopped", "local", "/b"),
            inventory.Session(
                "other", "same", "Name", "/cwd", "stopped", "local", "/a"
            ),
            inventory.Session(
                "host", "same", "Name", "/cwd", "stopped", "remote", "/a"
            ),
        ]
        self.assertEqual(len({item.key for item in variants}), 4)
        renamed = inventory.Session(
            "host", "same", "Other", "/cwd", "running", "local", "/a"
        )
        self.assertEqual(session.key, renamed.key)
        reassigned = inventory.Session(
            "host", "different", "Name", "/cwd", "stopped", "local", "/a"
        )
        moved = inventory.Session(
            "host", "same", "Name", "/different", "stopped", "local", "/a"
        )
        self.assertNotEqual(session.key, reassigned.key)
        self.assertNotEqual(session.key, moved.key)

    def test_local_filters_children_archives_residents_and_keeps_duplicate_ids(self):
        first = self.metadata(path="/a/session.jsonl")
        second = self.metadata(path="/b/session.jsonl")
        self.metadata("child", is_subagent=1)
        fork = self.metadata("parented", parent_session_path=first)
        self.metadata("archived", archived_at=self.now)
        self.metadata("architect")
        self.metadata("supervisor")
        self.metadata("resident", path="/state/supervisor-sessions/a.jsonl")
        self.metadata("resident2", path="/state/architect-sessions/a.jsonl")
        sessions = self.local()
        self.assertEqual({s.handle for s in sessions}, {first, second, fork})
        self.assertEqual({s.status for s in sessions}, {"stopped"})
        self.assertEqual(len({s.key for s in sessions}), 3)

    def test_names_use_metadata_first_message_then_cwd(self):
        self.metadata(
            "message", name="  ", first_message="Investigate the error\nDetails"
        )
        self.metadata("cwd", name=None, first_message="", cwd="/projects/widget")
        by_id = {s.session_id: s for s in self.local()}
        self.assertEqual(by_id["message"].name, "Investigate the error")
        self.assertIn("widget", by_id["cwd"].name)

    def test_local_window_title_uses_raw_name_and_bound_pid(self):
        path = self.metadata(name="Raw title", cwd="/projects/widget")
        child = self.process()
        self.bind(path, child.pid)
        session = self.local()[0]
        self.assertEqual(session.window_title, "π - Raw title - widget")
        self.assertEqual(session.pid, child.pid)
        self.conn.execute("UPDATE session_metadata SET name=NULL")
        self.conn.commit()
        unnamed = self.local()[0]
        self.assertEqual(unnamed.window_title, "π - widget")
        self.assertEqual(unnamed.name, "First user request")

    def test_missing_db_does_not_create_parent_or_database(self):
        missing = self.root / "absent" / "control.sqlite"
        self.assertEqual(inventory.load_local_sessions(missing, "host"), [])
        self.assertFalse(missing.parent.exists())

    def test_read_only_db_with_uri_characters_is_unchanged(self):
        self.metadata()
        self.conn.close()
        renamed = self.root / "control?#.sqlite"
        self.db.rename(renamed)
        before = renamed.read_bytes()
        self.assertEqual(len(inventory.load_local_sessions(renamed, "host")), 1)
        self.assertEqual(renamed.read_bytes(), before)
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["bin", "control?#.sqlite", "payload.json"],
        )

    def test_healthy_bound_live_process_is_running(self):
        path = self.metadata()
        child = self.process()
        self.bind(path, child.pid)
        self.assertEqual(self.local()[0].status, "running")
        self.assertIsNone(child.poll())

    def test_live_binding_with_unhealthy_or_stale_health_is_unknown(self):
        path = self.metadata()
        child = self.process()
        self.bind(path, child.pid)
        cases = [
            ("timeout", 7, self.now),
            ("dead", 7, self.now),
            ("ok", 6, self.now),
            ("ok", 7, (datetime.now(UTC) - timedelta(minutes=10)).isoformat()),
        ]
        for status, generation, checked in cases:
            with self.subTest(status=status, generation=generation, checked=checked):
                self.conn.execute(
                    "UPDATE session_health SET check_status=?, checked_generation=?, last_checked_at=?",
                    (status, generation, checked),
                )
                self.conn.commit()
                session = self.local()[0]
                self.assertEqual(session.status, "unknown")
                self.assertEqual(session.pid, child.pid)
        self.assertIsNone(child.poll())

    def test_live_duplicate_id_cannot_bind_wrong_project(self):
        first = self.metadata(path="/a/session.jsonl")
        second = self.metadata(path="/b/session.jsonl")
        child = self.process()
        self.bind(first, child.pid)
        by_path = {s.handle: s.status for s in self.local()}
        self.assertEqual(by_path, {first: "running", second: "unknown"})

    def test_uncertain_live_listener_is_unknown(self):
        path = self.metadata()
        child = self.process()
        for overrides in [
            {"runtime_instance_id": None},
            {
                "runtime_instance_id": json.dumps(
                    {"pid": child.pid, "startTimeTicks": 1}
                )
            },
            {"session_path_asserted_at": None},
            {"updated_at": "2000-01-01T00:00:00+00:00"},
        ]:
            with self.subTest(overrides=overrides):
                self.conn.execute("DELETE FROM runtime_mailbox_listeners")
                self.bind(path, child.pid, **overrides)
                self.assertEqual(self.local()[0].status, "unknown")

    def test_live_health_without_listener_is_unknown(self):
        path = self.metadata()
        child = self.process()
        self.bind(path, child.pid)
        self.conn.execute("DELETE FROM runtime_mailbox_listeners")
        self.conn.commit()
        self.assertEqual(self.local()[0].status, "unknown")

    def test_zombie_and_reaped_processes_are_stopped(self):
        path = self.metadata()
        child = self.process()
        self.bind(path, child.pid)
        child.terminate()
        deadline = time.monotonic() + 5
        while (
            Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            != "Z"
        ):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertEqual(self.local()[0].status, "stopped")
        child.wait(timeout=5)
        self.assertEqual(self.local()[0].status, "stopped")

    def test_remote_uses_registered_executable_and_rust_fields(self):
        self.remote_payload(
            [
                self.remote_row(),
                self.remote_row(name="stopped", running=False, server_running=False),
                self.remote_row(name="server-only", running=False),
                self.remote_row(name="inconsistent", server_running=False),
            ]
        )
        sessions = inventory.load_remote_sessions()
        self.assertEqual(
            [s.status for s in sessions], ["running", "stopped", "unknown", "unknown"]
        )
        self.assertEqual(sessions[0].handle, "managed-example")
        self.assertEqual(sessions[0].session_id, "conversation")
        self.assertEqual(sessions[0].cwd, "/projects/remote")
        self.assertEqual(sessions[0].backend, "remote")
        self.assertEqual(sessions[0].host, "agent-server")
        self.assertEqual(
            json.loads(self.requests.read_text()), ["--host", "agent-server", "list"]
        )

    def test_remote_malformed_data_fails_explicitly_without_retry(self):
        invalid = [
            "not json",
            "[]",
            "{}",
            '{"sessions": {}}',
            json.dumps({"sessions": [self.remote_row(running="false")]}),
            json.dumps({"sessions": [self.remote_row(conversation=None)]}),
            json.dumps({"sessions": [self.remote_row(name="")]}),
            '{"sessions": [], "errors": "bad"}',
            '{"sessions": [], "errors": [null]}',
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.requests.unlink(missing_ok=True)
                self.payload.write_text(payload)
                with self.assertRaises((ValueError, RuntimeError)):
                    inventory.load_remote_sessions()
                self.assertEqual(len(self.requests.read_text().splitlines()), 1)

    def test_transient_connection_failure_retries_and_succeeds(self):
        os.environ["INVENTORY_MODE"] = "transient"
        self.remote_payload([self.remote_row()])
        self.assertEqual(len(inventory.load_remote_sessions()), 1)
        self.assertEqual(len(self.requests.read_text().splitlines()), 3)

    def test_permanent_failure_does_not_retry(self):
        os.environ["INVENTORY_MODE"] = "permanent"
        with self.assertRaisesRegex((ValueError, RuntimeError), "Permission denied"):
            inventory.load_remote_sessions()
        self.assertEqual(len(self.requests.read_text().splitlines()), 1)

    def test_timeouts_are_bounded_and_reported(self):
        os.environ["INVENTORY_MODE"] = "timeout"
        with (
            patch.object(inventory, "_REMOTE_TIMEOUT", 0.1),
            self.assertRaisesRegex((ValueError, RuntimeError), "timed out|timeout"),
        ):
            inventory.load_remote_sessions()
        self.assertEqual(len(self.requests.read_text().splitlines()), 3)

    def test_root_errors_are_visible_without_discarding_local_results(self):
        self.metadata()
        self.remote_payload([self.remote_row()], ["project denied"])
        with self.assertRaisesRegex((ValueError, RuntimeError), "project denied"):
            inventory.load_remote_sessions()
        sessions, errors = inventory.collect_sessions()
        self.assertTrue(any(s.backend == "local" for s in sessions))
        self.assertTrue(any("project denied" in error for error in errors))

    def test_collect_keeps_remote_results_on_local_failure_and_sorts(self):
        self.conn.close()
        self.db.write_bytes(b"not a SQLite database")
        self.remote_payload(
            [self.remote_row(name="Zulu"), self.remote_row(name="Alpha")]
        )
        sessions, errors = inventory.collect_sessions()
        self.assertEqual([s.name for s in sessions], ["Alpha", "Zulu"])
        self.assertEqual(len(errors), 1)
        self.assertIn("local", errors[0].lower())

    def test_collect_keeps_local_on_missing_remote_executable(self):
        self.metadata()
        os.environ["PATH"] = str(self.root / "empty-bin")
        sessions, errors = inventory.collect_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].backend, "local")
        self.assertEqual(len(errors), 1)
        self.assertIn("remote", errors[0].lower())

    def test_local_control_db_path_uses_shared_environment_precedence(self):
        self.assertEqual(inventory.local_control_db_path(), self.db)
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg")
        self.assertEqual(inventory.local_control_db_path(), self.db)
        del os.environ["PI_CODING_AGENT_STATE_DIR"]
        self.assertEqual(
            inventory.local_control_db_path(), self.root / "xdg/pi/control.sqlite"
        )
        del os.environ["XDG_STATE_HOME"]
        os.environ["HOME"] = str(self.root / "home")
        self.assertEqual(
            inventory.local_control_db_path(),
            self.root / "home/.local/state/pi/control.sqlite",
        )

    def test_local_hosts_are_rejected_without_invoking_remote(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "must not use SSH"),
            ):
                inventory.load_remote_sessions(host)
        self.assertFalse(self.requests.exists())

    def test_remote_inventory_does_not_resolve_dns_locally(self):
        with patch(
            "socket.getfqdn",
            side_effect=AssertionError("local DNS must not block inventory"),
        ):
            self.assertEqual(inventory.load_remote_sessions(), [])

    def test_collect_default_db_resolution_and_merge(self):
        self.metadata(name="Zulu")
        self.remote_payload([self.remote_row(name="Alpha")])
        sessions, errors = inventory.collect_sessions()
        self.assertEqual(errors, [])
        self.assertEqual([s.name for s in sessions], ["Alpha", "Zulu"])
        state = self.root / "xdg"
        (state / "pi").mkdir(parents=True)
        self.conn.backup(sqlite3.connect(state / "pi" / "control.sqlite"))
        del os.environ["PI_CODING_AGENT_STATE_DIR"]
        os.environ["XDG_STATE_HOME"] = str(state)
        self.assertEqual(len(inventory.collect_sessions()[0]), 2)
        del os.environ["XDG_STATE_HOME"]
        home = self.root / "home"
        (home / ".local/state/pi").mkdir(parents=True)
        self.conn.backup(sqlite3.connect(home / ".local/state/pi/control.sqlite"))
        os.environ["HOME"] = str(home)
        self.assertEqual(len(inventory.collect_sessions()[0]), 2)


if __name__ == "__main__":
    unittest.main()

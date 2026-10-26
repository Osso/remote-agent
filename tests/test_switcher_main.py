"""Switcher CLI tests use isolated SQLite, real child processes and executable fixtures."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agent-switcher"


class SwitcherMainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="switcher-main-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.state = self.home / "state"
        self.state.mkdir()
        self.workspace = self.home / "project with spaces"
        self.workspace.mkdir()
        self.session_file = self.home / "conversation.jsonl"
        self.session_file.write_text("unused transcript fixture\n")
        self.identity = "01900000-1111-7222-8333-444444444444"
        self.events = self.home / "events.jsonl"
        self.remote = [
            {
                "name": "remote-work",
                "project": "sample",
                "cwd": "/remote/project",
                "conversation": "01900000-aaaa-7bbb-8ccc-dddddddddddd",
                "running": True,
                "server_running": True,
            }
        ]
        self.remote_file = self.home / "remote.json"
        self.remote_file.write_text(json.dumps({"sessions": self.remote}))
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "PI_CODING_AGENT_STATE_DIR": str(self.state),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        }
        self.write_command(
            "remote-agent",
            f"""import json,pathlib,sys
args=sys.argv[1:]
if args[-1]=='list': print(pathlib.Path({str(self.remote_file)!r}).read_text())
else:
 with pathlib.Path({str(self.events)!r}).open('a') as out: out.write(json.dumps({{'program':'remote-agent','args':args}})+'\\n')
""",
        )
        self.write_command(
            "pi",
            f"""import json,os,pathlib,sys
pathlib.Path({str(self.events)!r}).write_text(json.dumps({{'program':'pi','args':sys.argv[1:],'cwd':os.getcwd()}})+'\\n')
""",
        )
        self.write_command("niri", "import sys\nprint('[]')\n")
        self.create_database()

    def write_command(self, name, body):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(0o755)

    def create_database(self):
        with sqlite3.connect(self.state / "control.sqlite") as db:
            db.executescript("""
                CREATE TABLE session_metadata (session_path TEXT PRIMARY KEY,id TEXT,cwd TEXT,name TEXT,parent_session_path TEXT,archived_at TEXT,goal_json TEXT,is_subagent INTEGER,subagent_name TEXT,model_provider TEXT,model_id TEXT,thinking_level TEXT,created_at TEXT,modified_at TEXT,message_count INTEGER,first_message TEXT,updated_at TEXT);
                CREATE TABLE session_health (session_id TEXT PRIMARY KEY,agent_generation INTEGER,pid INTEGER,last_active_at TEXT,last_checked_at TEXT,check_status TEXT,checked_generation INTEGER,check_latency_ms INTEGER,updated_at TEXT);
                CREATE TABLE runtime_mailbox_listeners (recipient_session_id TEXT,recipient_agent_id_key TEXT,pid INTEGER,runtime_instance_id TEXT,session_path TEXT,session_path_asserted_at TEXT,updated_at TEXT);
            """)
            db.execute(
                "INSERT INTO session_metadata (session_path,id,cwd,name,is_subagent,created_at,modified_at,message_count,first_message,updated_at) VALUES (?,?,?,?,0,?,?,1,'question',?)",
                (
                    str(self.session_file),
                    self.identity,
                    str(self.workspace),
                    "Local research",
                    *(["2026-10-25T12:00:00Z"] * 3),
                ),
            )

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def inventory(self):
        result = self.run_cli("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def entry_key(self, backend):
        entry = next(
            entry
            for entry in self.inventory()["sessions"]
            if entry["backend"] == backend
        )
        return entry["key"]

    def test_lists_both_hosts_without_reading_or_changing_transcripts(self):
        original = self.session_file.read_bytes()
        listed = self.inventory()
        self.assertEqual(
            {entry["host"] for entry in listed["sessions"]},
            {socket.gethostname(), "agent-server"},
        )
        self.assertEqual(
            {entry["name"] for entry in listed["sessions"]},
            {"Local research", "remote-work"},
        )
        self.assertEqual(self.session_file.read_bytes(), original)
        self.assertFalse(self.events.exists())

    def test_stopped_local_uses_native_session_path_and_project(self):
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(self.events.read_text())
        self.assertEqual(
            event,
            {
                "program": "pi",
                "args": ["--session", str(self.session_file)],
                "cwd": str(self.workspace),
            },
        )

    def test_remote_selection_reattaches_without_start_or_resume(self):
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.events.read_text()),
            {
                "program": "remote-agent",
                "args": ["--host", "agent-server", "attach", "remote-work"],
            },
        )

    def test_remote_stopped_record_never_invents_a_replacement(self):
        key = self.entry_key("remote")
        self.remote[0].update(running=False, server_running=False)
        self.remote_file.write_text(json.dumps({"sessions": self.remote}))
        result = self.run_cli("--open", key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped", result.stderr.lower())
        self.assertFalse(self.events.exists())

    def test_unknown_or_stale_selection_never_launches(self):
        result = self.run_cli("--open", "unrecognized selection")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.events.exists())

    def stop_child(self, child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    def bind_process(self, child):
        timestamp = datetime.now(UTC).isoformat()
        stat = Path(f"/proc/{child.pid}/stat").read_text()
        identity = json.dumps(
            {
                "pid": child.pid,
                "startTimeTicks": int(stat.rsplit(")", 1)[1].split()[19]),
                "incarnation": "fixture",
            }
        )
        with sqlite3.connect(self.state / "control.sqlite") as db:
            db.execute(
                "INSERT INTO session_health VALUES (?,1,?,NULL,?,'ok',1,NULL,?)",
                (self.identity, child.pid, timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO runtime_mailbox_listeners VALUES (?,'',?,?,?, ?,?)",
                (
                    self.identity,
                    child.pid,
                    identity,
                    str(self.session_file),
                    timestamp,
                    timestamp,
                ),
            )

    def test_live_local_without_visible_window_is_not_resumed(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop_child, child)
        self.bind_process(child)
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already open", result.stderr.lower())
        self.assertFalse(self.events.exists())

    def test_foreground_ghostty_session_focus_preserves_the_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop_child, child)
        self.bind_process(child)
        windows = [
            {
                "id": 42,
                "title": "π - Local research - project with spaces",
                "app_id": "com.mitchellh.ghostty",
                "pid": os.getpid(),
            }
        ]
        self.write_command(
            "niri",
            f"""import json,pathlib,sys
if sys.argv[1:]==['msg','--json','windows']: print({json.dumps(windows)!r})
else: pathlib.Path({str(self.events)!r}).write_text(json.dumps({{'program':'niri','args':sys.argv[1:]}})+'\\n')
""",
        )
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(child.poll())
        self.assertEqual(
            json.loads(self.events.read_text()),
            {
                "program": "niri",
                "args": ["msg", "action", "focus-window", "--id", "42"],
            },
        )

    def test_desktop_launcher_opens_the_selector_not_a_new_pi(self):
        self.write_command(
            "ghostty",
            f"""import json,pathlib,sys
pathlib.Path({str(self.events)!r}).write_text(json.dumps({{'program':'ghostty','args':sys.argv[1:]}})+'\\n')
""",
        )
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.events.read_text()),
            {"program": "ghostty", "args": ["-e", str(SCRIPT), "--pick"]},
        )

    def test_reassigned_remote_name_invalidates_the_old_selection(self):
        key = self.entry_key("remote")
        self.remote[0]["conversation"] = "01900000-ffff-7eee-8ddd-cccccccccccc"
        self.remote_file.write_text(json.dumps({"sessions": self.remote}))
        result = self.run_cli("--open", key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr.lower())
        self.assertFalse(self.events.exists())

    def test_a_matching_title_with_the_wrong_process_is_not_focused(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop_child, child)
        self.bind_process(child)
        windows = [
            {
                "id": 42,
                "title": "π - Local research - project with spaces",
                "app_id": "com.mitchellh.ghostty",
                "pid": 4294967295,
            }
        ]
        self.write_command("niri", f"print({json.dumps(windows)!r})\n")
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(child.poll())
        self.assertFalse(self.events.exists())

    def test_malformed_window_inventory_reports_context_without_launching(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop_child, child)
        self.bind_process(child)
        self.write_command("niri", "print('{}')\n")
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid window inventory", result.stderr.lower())
        self.assertFalse(self.events.exists())


if __name__ == "__main__":
    unittest.main()

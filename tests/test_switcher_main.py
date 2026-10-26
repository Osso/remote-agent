"""Switcher CLI tests use isolated SQLite, real child processes and executable fixtures."""

import json
import os
import pty
import select
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "agent-switcher"
WEZTERM_APP = "org.wezfurlong.wezterm"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


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
with pathlib.Path({str(self.events)!r}).open('a') as out: out.write(json.dumps({{'program':'pi','args':sys.argv[1:],'cwd':os.getcwd()}})+'\\n')
""",
        )
        self.panes_file = self.home / "panes.json"
        self.set_panes([self.pane(30, window=3, tty="/dev/pts/unrelated")])
        self.write_command(
            "wezterm",
            f"""import json,pathlib,sys
args=sys.argv[1:]
if args[-3:]==['list','--format','json']: print(pathlib.Path({str(self.panes_file)!r}).read_text())
else:
 with pathlib.Path({str(self.events)!r}).open('a') as out: out.write(json.dumps({{'program':'wezterm','args':args}})+'\\n')
 if 'spawn' in args: print(77)
""",
        )
        self.set_windows([{"id": 92, "app_id": WEZTERM_APP, "is_focused": False}])
        self.create_database()

    def pane(self, pane_id, window, tty):
        return {
            "window_id": window,
            "tab_id": pane_id,
            "pane_id": pane_id,
            "tty_name": tty,
        }

    def set_panes(self, panes):
        self.panes_file.write_text(json.dumps(panes))

    def set_windows(self, windows):
        self.write_command(
            "niri",
            f"""import json,pathlib,sys
if sys.argv[1:]==['msg','--json','windows']: print({json.dumps(windows)!r})
else:
 with pathlib.Path({str(self.events)!r}).open('a') as out: out.write(json.dumps({{'program':'niri','args':sys.argv[1:]}})+'\\n')
""",
        )

    def recorded(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def terminal_process(self, *argv):
        controller, terminal = pty.openpty()
        self.addCleanup(os.close, controller)
        child = subprocess.Popen(argv or SLEEPER, stdin=terminal)
        os.close(terminal)
        self.addCleanup(self.stop_child, child)
        return child, os.readlink(f"/proc/{child.pid}/fd/0")

    def attach_process(self, *args):
        attached = self.home / "attached"
        attached.mkdir(exist_ok=True)
        client = attached / "remote-agent"
        client.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        client.chmod(0o755)
        return self.terminal_process(
            str(client), *(args or ("--host", "agent-server", "attach", "remote-work"))
        )

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
        self.assertEqual(self.recorded(), [])

    def test_stopped_local_resumes_in_one_new_tab_of_the_main_window(self):
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.recorded(),
            [
                {
                    "program": "wezterm",
                    "args": [
                        "cli",
                        "--no-auto-start",
                        "spawn",
                        "--window-id",
                        "3",
                        "--cwd",
                        str(self.workspace),
                        "--",
                        "pi",
                        "--session",
                        str(self.session_file),
                    ],
                },
                {
                    "program": "niri",
                    "args": ["msg", "action", "focus-window", "--id", "92"],
                },
            ],
        )

    def test_unattached_remote_opens_one_attach_tab_without_start_or_resume(self):
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.recorded(),
            [
                {
                    "program": "wezterm",
                    "args": [
                        "cli",
                        "--no-auto-start",
                        "spawn",
                        "--window-id",
                        "3",
                        "--",
                        "remote-agent",
                        "--host",
                        "agent-server",
                        "attach",
                        "remote-work",
                    ],
                },
                {
                    "program": "niri",
                    "args": ["msg", "action", "focus-window", "--id", "92"],
                },
            ],
        )

    def test_without_a_wezterm_window_the_session_opens_a_new_window(self):
        self.set_panes([])
        self.set_windows([])
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.recorded(),
            [
                {
                    "program": "wezterm",
                    "args": [
                        "cli",
                        "--no-auto-start",
                        "spawn",
                        "--new-window",
                        "--",
                        "remote-agent",
                        "--host",
                        "agent-server",
                        "attach",
                        "remote-work",
                    ],
                }
            ],
        )

    def test_remote_stopped_record_never_invents_a_replacement(self):
        key = self.entry_key("remote")
        self.remote[0].update(running=False, server_running=False)
        self.remote_file.write_text(json.dumps({"sessions": self.remote}))
        result = self.run_cli("--open", key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopped", result.stderr.lower())
        self.assertEqual(self.recorded(), [])

    def test_unknown_or_stale_selection_never_launches(self):
        result = self.run_cli("--open", "unrecognized selection")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.recorded(), [])

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

    def test_running_local_in_a_hidden_tab_activates_that_exact_pane(self):
        child, terminal = self.terminal_process()
        self.bind_process(child)
        self.set_panes(
            [
                self.pane(30, window=3, tty="/dev/pts/unrelated"),
                self.pane(31, window=3, tty=terminal),
            ]
        )
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(child.poll())
        self.assertEqual(
            self.recorded(),
            [
                {
                    "program": "wezterm",
                    "args": [
                        "cli",
                        "--no-auto-start",
                        "activate-pane",
                        "--pane-id",
                        "31",
                    ],
                },
                {
                    "program": "niri",
                    "args": ["msg", "action", "focus-window", "--id", "92"],
                },
            ],
        )

    def test_attached_remote_tab_is_activated_without_another_attach(self):
        client, terminal = self.attach_process()
        self.set_panes(
            [
                self.pane(30, window=3, tty="/dev/pts/unrelated"),
                self.pane(32, window=3, tty=terminal),
            ]
        )
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(client.poll())
        self.assertEqual(
            self.recorded(),
            [
                {
                    "program": "wezterm",
                    "args": [
                        "cli",
                        "--no-auto-start",
                        "activate-pane",
                        "--pane-id",
                        "32",
                    ],
                },
                {
                    "program": "niri",
                    "args": ["msg", "action", "focus-window", "--id", "92"],
                },
            ],
        )

    def test_attachment_of_another_remote_name_is_not_selected(self):
        self.attach_process("--host", "agent-server", "attach", "remote-work-2")
        self.attach_process("--host", "desktop", "attach", "remote-work")
        self.attach_process("attach", "remote-work", "--user", "allison")
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded()[0]["args"][2], "spawn")

    def test_attach_with_trailing_or_default_host_is_the_same_session(self):
        for args in (
            ("attach", "remote-work", "--host", "agent-server"),
            ("attach", "remote-work", "--host=agent-server"),
            ("attach", "remote-work"),
        ):
            with self.subTest(args=args):
                client, terminal = self.attach_process(*args)
                result = self.run_cli("--open", self.entry_key("remote"))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"outside wezterm on {terminal}", result.stderr.lower())
                self.assertEqual(self.recorded(), [])
                self.stop_child(client)

    def test_session_open_outside_wezterm_is_not_duplicated(self):
        child, terminal = self.terminal_process()
        self.bind_process(child)
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside wezterm", result.stderr.lower())
        self.assertIn(terminal, result.stderr)
        self.assertIsNone(child.poll())
        self.assertEqual(self.recorded(), [])

    def test_remote_attached_in_two_tabs_is_ambiguous(self):
        _, first = self.attach_process()
        _, second = self.attach_process()
        self.set_panes(
            [self.pane(31, window=3, tty=first), self.pane(32, window=3, tty=second)]
        )
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ambiguous", result.stderr.lower())
        self.assertEqual(self.recorded(), [])

    def test_live_local_without_a_terminal_is_not_resumed(self):
        child = subprocess.Popen(SLEEPER, stdin=subprocess.DEVNULL)
        self.addCleanup(self.stop_child, child)
        self.bind_process(child)
        result = self.run_cli("--open", self.entry_key("local"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already open", result.stderr.lower())
        self.assertEqual(self.recorded(), [])

    def test_picker_window_is_neither_spawn_target_nor_focused(self):
        self.environment["WEZTERM_PANE"] = "50"
        self.set_panes(
            [
                self.pane(50, window=5, tty="/dev/pts/picker"),
                self.pane(30, window=3, tty="/dev/pts/unrelated"),
            ]
        )
        self.set_windows(
            [
                {"id": 91, "app_id": WEZTERM_APP, "is_focused": True},
                {"id": 92, "app_id": WEZTERM_APP, "is_focused": False},
                {"id": 93, "app_id": "firefox", "is_focused": False},
            ]
        )
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertEqual(result.returncode, 0, result.stderr)
        spawn, focus = self.recorded()
        self.assertEqual(spawn["args"][3:5], ["--window-id", "3"])
        self.assertEqual(focus["args"], ["msg", "action", "focus-window", "--id", "92"])

    def test_two_unidentified_wezterm_windows_are_not_guessed(self):
        self.set_windows(
            [
                {"id": 91, "app_id": WEZTERM_APP, "is_focused": False},
                {"id": 92, "app_id": WEZTERM_APP, "is_focused": False},
            ]
        )
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2 wezterm windows", result.stderr.lower())
        self.assertNotIn("niri", [event["program"] for event in self.recorded()])

    def test_desktop_launcher_opens_the_selector_in_wezterm_not_a_new_pi(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.recorded(),
            [{"program": "wezterm", "args": ["start", "--", str(SCRIPT), "--pick"]}],
        )

    def test_reassigned_remote_name_invalidates_the_old_selection(self):
        key = self.entry_key("remote")
        self.remote[0]["conversation"] = "01900000-ffff-7eee-8ddd-cccccccccccc"
        self.remote_file.write_text(json.dumps({"sessions": self.remote}))
        result = self.run_cli("--open", key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr.lower())
        self.assertEqual(self.recorded(), [])

    def test_malformed_pane_inventory_reports_context_without_launching(self):
        self.panes_file.write_text("{}")
        result = self.run_cli("--open", self.entry_key("remote"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid wezterm pane inventory", result.stderr.lower())
        self.assertEqual(self.recorded(), [])

    def test_fatal_inventory_error_stays_visible_until_enter(self):
        (self.state / "control.sqlite").unlink()
        self.remote_file.write_text(
            json.dumps({"sessions": [], "errors": ["agent-server unavailable"]})
        )
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve(
                sys.executable,
                [sys.executable, str(SCRIPT), "--pick"],
                self.environment,
            )
        output = bytearray()
        reaped = False
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(terminal, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                    if b"Press Enter to close" in output:
                        break
            self.assertIn(b"agent-server unavailable", output)
            self.assertIn(b"Press Enter to close", output)
            self.assertEqual(os.waitpid(pid, os.WNOHANG)[0], 0)
            os.write(terminal, b"\n")
            _, status = os.waitpid(pid, 0)
            reaped = True
            self.assertEqual(os.waitstatus_to_exitcode(status), 1)
        finally:
            os.close(terminal)
            if not reaped:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()

"""Worker process boundary only; fake installed Pi never loads a provider."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = "\x1b[31mpre-compaction\x1b[0m\r\n" + "".join(
    f"\x1b[38;2;20;40;60mline {number:03d}\x1b[0m\n" for number in range(1, 241)
)
FIXTURE = f"""#!{sys.executable}
import json, os, pathlib, sys
home = pathlib.Path(os.environ['HOME'])
with (home / 'invocations.jsonl').open('a') as capture:
    capture.write(json.dumps({{'executable': sys.argv[0], 'argv': sys.argv[1:],
        'cwd': os.getcwd(), 'env': {{key: os.environ.get(key) for key in
        ('HOME', 'PATH', 'PI_CODING_AGENT_DIR', 'TERM', 'COLORTERM')}}}}) + '\\n')
if pathlib.Path(sys.argv[0]).name == 'tmux' and sys.argv[1] == 'display-message':
    state = (home / 'tmux-state').read_text()
    if state == 'missing':
        sys.stderr.write('no server running')
        sys.exit(1)
    print(str(home / 'project') + '\\t' + ('0\\t' if state == 'live' else '1\\t7'))
    sys.exit(0)
if pathlib.Path(sys.argv[0]).name != 'pi' or len(sys.argv) != 5 or sys.argv[1] != '--export-terminal' or sys.argv[3] != '--export-columns':
    sys.stderr.write('fixture forbids agent start or lifecycle operations')
    sys.exit(91)
if sys.argv[2] == 'not-saved':
    sys.stderr.write('session id not found: not-saved')
    sys.exit(4)
if sys.argv[2] == 'export-error':
    sys.stdout.write('partial output must not become success')
    sys.stderr.write('fixture export failed: corrupt saved branch')
    sys.exit(7)
source = pathlib.Path(sys.argv[2]) if sys.argv[2].startswith('/') else home / 'saved-history.ansi'
try:
    sys.stdout.buffer.write(source.read_bytes())
except FileNotFoundError:
    sys.stderr.write('snapshot missing: ' + str(source))
    sys.exit(2)
"""


class WorkerHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ra-history-")
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True)
        (config / "remote-hosts.json").write_text(
            json.dumps({"desktop": {"cargo_build_jobs": "3"}})
        )
        (config / "remote-projects.json").write_text(
            json.dumps({"fixture": {"remote": "project"}})
        )
        (self.home / "project").mkdir()
        self.bindir = self.home / ".local/bin"
        self.bindir.mkdir(parents=True)
        for name in ("pi", "tmux", "detach", "systemd-run", "systemctl", "i"):
            binary = self.bindir / name
            binary.write_text(FIXTURE)
            binary.chmod(0o700)
        self.source = self.home / "saved-history.ansi"
        self.source.write_bytes(HISTORY.encode())
        self.directory = self.home / ".local/state/remote-agent/remote-agent-demo"
        self.directory.mkdir(parents=True, mode=0o700)
        self.directory.parent.chmod(0o700)
        self.record = self.directory / "session.json"
        self.write_record()
        runtime = self.directory / "runtime.json"
        runtime.write_text('{"fixture":"no live agent"}')
        runtime.chmod(0o600)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.directory / "dtach.sock"))
        (self.directory / "dtach.sock").chmod(0o600)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.bindir),
            "PI_CODING_AGENT_DIR": "/wrong/inherited/pi",
            "TERM": "dumb",
            "COLORTERM": "truecolor",
        }

    def write_record(self, **changes):
        record = {
            "repo": "fixture",
            "agent": "pi",
            "backend": "dtach",
            "conversation": "fixture-conversation",
            "colorterm": "truecolor",
            **changes,
        }
        self.record.write_text(json.dumps(record))
        self.record.chmod(0o600)

    def snapshot(self):
        paths = [
            self.directory.parent,
            self.directory,
            *self.directory.iterdir(),
            self.source,
        ]
        return {
            str(path): (
                path.stat().st_mode,
                path.stat().st_ino,
                path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in paths
        }

    def invoke(self, **changes):
        before = self.snapshot()
        result = subprocess.run(
            [sys.executable, str(ROOT / "remote-agent-worker.py")],
            input=json.dumps(
                {"action": "history", "name": "demo", "columns": 93, **changes}
            ),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            self.snapshot(), before, "history changed managed state/source"
        )
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def calls(self):
        capture = self.home / "invocations.jsonl"
        return (
            [json.loads(line) for line in capture.read_text().splitlines()]
            if capture.exists()
            else []
        )

    def test_exports_complete_ansi_with_exact_installed_command_and_environment(self):
        for backend, conversation, columns, colorterm, expected_color in (
            ("dtach", "fixture-conversation", 93, "truecolor", "truecolor"),
            ("tmux", str(self.source), 120, "24bit", "truecolor"),
            ("dtach", "fixture-conversation", 1, "", None),
        ):
            with self.subTest(backend=backend, colorterm=colorterm):
                self.write_record(backend=backend, conversation=conversation)
                code, response = self.invoke(columns=columns, colorterm=colorterm)
                self.assertEqual(
                    (code, response),
                    (0, {"available": True, "history": HISTORY, "project": "fixture"}),
                )
                self.assertEqual(
                    self.calls()[-1],
                    {
                        "executable": str(self.bindir / "pi"),
                        "argv": [
                            "--export-terminal",
                            conversation,
                            "--export-columns",
                            str(columns),
                        ],
                        "cwd": str(self.home / "project"),
                        "env": {
                            "HOME": str(self.home),
                            "PATH": f"{self.bindir}:{self.home}/.cargo/bin:{self.bindir}",
                            "PI_CODING_AGENT_DIR": str(self.home / ".config/pi/agent"),
                            "TERM": "xterm-256color",
                            "COLORTERM": expected_color,
                        },
                    },
                )
        self.assertEqual(len(self.calls()), 3)

    def test_project_pi_executable_routes_history_with_unchanged_context(self):
        code, response = self.invoke()
        self.assertEqual(code, 0)
        default_call = self.calls()[-1]
        wrapper = self.home / "host wrappers/pi"
        wrapper.parent.mkdir()
        wrapper.write_text(FIXTURE)
        wrapper.chmod(0o700)
        projects = self.home / ".config/remote-agent/remote-projects.json"
        projects.write_text(
            json.dumps(
                {"fixture": {"remote": "project", "pi_executable": str(wrapper)}}
            )
        )
        code, configured_response = self.invoke()
        self.assertEqual((code, configured_response), (0, response))
        self.assertEqual(self.calls()[-1], {**default_call, "executable": str(wrapper)})

    def test_invalid_project_pi_executable_history_fails_without_default_export(self):
        projects = self.home / ".config/remote-agent/remote-projects.json"
        projects.write_text(
            json.dumps(
                {
                    "fixture": {
                        "remote": "project",
                        "pi_executable": str(self.home / "missing"),
                    }
                }
            )
        )
        code, response = self.invoke()
        self.assertNotEqual(code, 0)
        self.assertIn("pi_executable", response["error"])
        self.assertEqual(self.calls(), [])

    def test_omitted_color_does_not_inherit_worker_or_record_truecolor(self):
        code, response = self.invoke()
        self.assertEqual(
            (code, response),
            (0, {"available": True, "history": HISTORY, "project": "fixture"}),
        )
        self.assertIsNone(self.calls()[0]["env"]["COLORTERM"])

    def test_unavailable_without_invoking_harness_for_nonpi_or_missing_conversation(
        self,
    ):
        for changes, reason in (
            ({"agent": "claude"}, "pi"),
            ({"conversation": ""}, "conversation"),
            ({"conversation": None}, "conversation"),
        ):
            with self.subTest(changes=changes):
                self.write_record(**changes)
                code, response = self.invoke()
                self.assertEqual(code, 0)
                self.assertIs(response["available"], False)
                self.assertIn(reason, response["reason"].lower())
                self.assertEqual(response["project"], "fixture")
        self.write_record()
        record = json.loads(self.record.read_text())
        del record["conversation"]
        self.record.write_text(json.dumps(record))
        code, response = self.invoke()
        self.assertEqual(code, 0)
        self.assertIs(response["available"], False)
        self.assertIn("conversation", response["reason"])
        self.record.unlink()
        code, response = self.invoke()
        self.assertEqual(code, 0)
        self.assertIs(response["available"], False)
        self.assertIn("record", response["reason"])
        self.assertIsNone(response["project"])
        self.assertEqual(self.calls(), [])

    def test_invalid_columns_and_colors_fail_without_harness(self):
        for columns in (True, False, 0, -1, 1.5, "80", None):
            with self.subTest(columns=columns):
                code, response = self.invoke(columns=columns)
                self.assertEqual(code, 1)
                self.assertIn("columns", response["error"])
        for color in ("TrUeCoLoR", "invalid", None, True, []):
            with self.subTest(color=color):
                code, response = self.invoke(colorterm=color)
                self.assertEqual(code, 1)
                self.assertIn("colorterm", response["error"])
        self.assertEqual(self.calls(), [])

    def test_export_failure_preserves_stderr_and_does_not_return_partial_history(self):
        self.write_record(conversation="export-error")
        code, response = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("fixture export failed: corrupt saved branch", response["error"])
        self.assertNotIn("history", response)
        self.assertEqual(len(self.calls()), 1)

    def test_missing_snapshot_is_an_explicit_error_not_unavailable(self):
        missing = str(self.home / "missing.jsonl")
        self.write_record(conversation=missing)
        code, response = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("snapshot missing: " + missing, response["error"])
        self.assertNotIn("available", response)
        self.assertEqual(len(self.calls()), 1)

    def test_only_exit_four_with_confirmed_live_agent_is_initial_empty_history(self):
        for backend in ("dtach", "tmux"):
            for state in ("live", "dead", "missing"):
                with self.subTest(backend=backend, state=state):
                    self.write_record(backend=backend, conversation="not-saved")
                    fields = (
                        Path(f"/proc/{os.getpid()}/stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()
                    )
                    process = {
                        "pid": os.getpid(),
                        "start": fields[19],
                        "boot": Path("/proc/sys/kernel/random/boot_id")
                        .read_text()
                        .strip(),
                    }
                    runtime = (
                        {}
                        if state == "missing"
                        else {"process": process if state == "live" else None}
                    )
                    (self.directory / "runtime.json").write_text(json.dumps(runtime))
                    (self.home / "tmux-state").write_text(state)
                    code, response = self.invoke()
                    if state == "live":
                        self.assertEqual(
                            (code, response),
                            (
                                0,
                                {
                                    "available": False,
                                    "reason": "No saved conversation history yet.",
                                    "project": "fixture",
                                },
                            ),
                        )
                    else:
                        self.assertEqual(code, 1)
                        self.assertIn(
                            "session id not found: not-saved", response["error"]
                        )
            # A different export failure must never be treated as initial empty history.
            self.write_record(backend=backend, conversation="export-error")
            (self.directory / "runtime.json").write_text(
                json.dumps({"process": process})
            )
            (self.home / "tmux-state").write_text("live")
            code, response = self.invoke()
            self.assertEqual(code, 1)
            self.assertIn("corrupt saved branch", response["error"])
        self.assertTrue(
            all(
                call["argv"][0] in ("--export-terminal", "display-message")
                for call in self.calls()
            )
        )

    def test_missing_installed_pi_fails_without_lifecycle_fallback(self):
        (self.bindir / "pi").unlink()
        code, response = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("agent executable not found: pi", response["error"])
        self.assertEqual(self.calls(), [])

    def test_private_record_permissions_remain_enforced(self):
        self.record.chmod(0o644)
        code, response = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("unexpected filesystem state", response["error"])
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()

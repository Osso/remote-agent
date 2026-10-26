"""Picker behavior through an executable fzf stand-in, without a real terminal."""

import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.fzf = self.directory / "fzf"
        self.record = self.directory / "record.json"
        self.write_fzf()
        self.environment = patch.dict(
            os.environ,
            {
                "PATH": str(self.directory),
                "PRIVATE_PICKER_TEST_SECRET": "do-not-pass",
                "FZF_DEFAULT_OPTS": "--preview 'touch /tmp/unsafe' --filter unwanted",
                "FZF_DEFAULT_OPTS_FILE": "/tmp/untrusted-fzf-config",
                "FZF_DEFAULT_COMMAND": "touch /tmp/unsafe",
                "SHELL": "/tmp/untrusted-shell",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_fzf(self, result="print(rows[1])", exit_code=0):
        self.fzf.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys\n"
            "data = sys.stdin.read()\n"
            "rows = data.splitlines()\n"
            f"pathlib.Path({str(self.record)!r}).write_text(json.dumps(\n"
            "    {'stdin': data, 'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
            f"{result}\n"
            f"sys.exit({exit_code})\n"
        )
        self.fzf.chmod(0o755)

    def session(self, handle, **changes):
        fields = dict(
            host="aso",
            session_id="conversation " + handle,
            name="same name 日本語",
            cwd="/projects/a path",
            status="running",
            backend="pi",
            handle=handle,
        )
        fields.update(changes)
        # The inventory contract exposes an opaque key; the picker must not decode it.
        fields["key"] = (fields["host"], fields["backend"], handle)
        return SimpleNamespace(**fields)

    def choose(self, sessions, errors=None):
        try:
            picker = importlib.import_module("switcher.picker")
        except ModuleNotFoundError as error:
            self.fail(f"Picker API is not implemented: {error}")
        return picker.choose_session(sessions, errors or [])

    def recorded(self):
        return json.loads(self.record.read_text())

    def test_selects_duplicate_unicode_name_by_opaque_identity(self):
        first = self.session("first handle")
        second = self.session("second handle", host="agent-server", status="stopped")
        self.assertIs(self.choose([first, second]), second)
        record = self.recorded()
        rows = record["stdin"].splitlines()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].split("\t")[0], rows[1].split("\t")[0])
        self.assertIn("same name 日本語", rows[0])
        self.assertIn("agent-server", rows[1])
        self.assertIn("stopped", rows[1])
        self.assertIn("/projects/a path", rows[0])
        header = record["argv"][record["argv"].index("--header") + 1]
        for label in ("NAME", "HOST", "STATUS", "PATH", "Enter select", "Esc cancel"):
            self.assertIn(label, header)
        self.assertIn("--no-height", record["argv"])
        self.assertIn("--with-nth=2..", record["argv"])

    def test_aligns_columns_and_keeps_selectors_stable_when_reordered(self):
        sessions = [
            self.session("one", name="a"),
            self.session("two", name="long name"),
        ]
        self.choose(sessions)
        rows = self.recorded()["stdin"].splitlines()
        self.assertEqual(rows[0].index("aso"), rows[1].index("aso"))
        self.choose(list(reversed(sessions)))
        self.assertEqual(self.recorded()["stdin"].splitlines(), list(reversed(rows)))

    def test_sanitizes_all_display_fields_and_displays_inventory_errors(self):
        bad = "unsafe\x1b[31m\n\r\t\x00\x7f\x85\u202e"
        session = self.session("one", name=bad, host=bad, cwd=bad, status="unknown")
        self.write_fzf("print(rows[0])")
        self.assertIs(self.choose([session], ["agent-server: " + bad]), session)
        record = self.recorded()
        self.assertEqual(len(record["stdin"].splitlines()), 1)
        header = record["argv"][record["argv"].index("--header") + 1]
        self.assertIn("agent-server:", header)
        self.assertIn("unknown", record["stdin"])
        for control in ("\x1b", "\r", "\x00", "\x7f", "\x85", "\u202e"):
            self.assertNotIn(control, record["stdin"] + header)
        self.assertNotIn("unsafe\n", header)
        self.assertNotIn("unsafe\t", record["stdin"])

    def test_does_not_leak_secrets_or_inherit_fzf_commands(self):
        self.write_fzf("print(rows[0])")
        self.choose([self.session("one")])
        env = self.recorded()["env"]
        for key in (
            "PRIVATE_PICKER_TEST_SECRET",
            "FZF_DEFAULT_OPTS",
            "FZF_DEFAULT_OPTS_FILE",
            "FZF_DEFAULT_COMMAND",
            "SHELL",
        ):
            self.assertNotIn(key, env)

    def test_cancel_exit_codes_return_none(self):
        for code in (1, 130):
            with self.subTest(code=code):
                self.write_fzf("pass", code)
                self.assertIsNone(self.choose([self.session("one")]))

    def test_other_exit_codes_raise_with_sanitized_fzf_error(self):
        self.write_fzf("print('invalid option\\x1b[31m', file=sys.stderr)", 2)
        with self.assertRaisesRegex(RuntimeError, "fzf.*2.*invalid option") as error:
            self.choose([self.session("one")])
        self.assertNotIn("\x1b", str(error.exception))

    def test_missing_fzf_is_contextual(self):
        self.fzf.unlink()
        with self.assertRaisesRegex(RuntimeError, "fzf"):
            self.choose([self.session("one")])

    def test_success_with_stale_or_empty_or_modified_selection_fails(self):
        for result in (
            "print('stale selector\\told row')",
            "pass",
            "print(rows[0] + ' changed')",
            "print(rows[0]); print(rows[0])",
        ):
            with self.subTest(result=result):
                self.write_fzf(result)
                with self.assertRaisesRegex(RuntimeError, "selection"):
                    self.choose([self.session("one")])

    def test_long_names_keep_host_and_state_visible_without_changing_identity(self):
        session = self.session("long", name="Very long session title " * 12)
        self.write_fzf("print(rows[0])")
        self.assertIs(self.choose([session]), session)
        row = self.recorded()["stdin"].split("\t", 1)[1]
        self.assertLess(row.index("aso"), 60)
        self.assertIn("…", row)
        self.assertIn("running", row)

    def test_empty_healthy_inventory_reports_no_sessions_without_fzf(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.assertIsNone(self.choose([]))
        self.assertIn("No sessions", output.getvalue())
        self.assertFalse(self.record.exists())

    def test_empty_failed_inventory_raises_with_host_error_without_fzf(self):
        with self.assertRaisesRegex(RuntimeError, "agent-server: connection refused"):
            self.choose([], ["agent-server: connection refused"])
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()

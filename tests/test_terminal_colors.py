"""Controller color forwarding through fixture SSH/mosh subprocesses only."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLOR_CASES = (
    ("truecolor", "truecolor"),
    ("TrUeCoLoR", "truecolor"),
    ("24bit", "truecolor"),
    ("24BiT", "truecolor"),
    (None, ""),
    ("", ""),
    ("unrecognized", ""),
)


class ControllerTerminalColorsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True)
        (config / "remote-hosts.json").write_text(
            json.dumps(
                {
                    "agent-server": {
                        "ssh": "fixture-server",
                        "home": "/home/fixture",
                        "wrap": [],
                        "windows_shell": False,
                    }
                }
            )
        )
        (config / "remote-projects.json").write_text(
            json.dumps({"engine": {"remote": "Projects/engine", "local": "/engine"}})
        )
        self.capture = self.home / "transport.jsonl"
        bindir = self.home / "bin"
        bindir.mkdir()
        fixture = f"""#!{sys.executable}
import json, os, pathlib, sys
transport = pathlib.Path(sys.argv[0]).name
record = {{"transport": transport, "argv": sys.argv[1:]}}
if transport == "ssh":
    record["request"] = json.load(sys.stdin)
with open(os.environ["COLOR_TEST_CAPTURE"], "a") as output:
    output.write(json.dumps(record) + "\\n")
if transport == "ssh":
    print(json.dumps({{"sessions": [], "name": "demo"}}))
"""
        for name in ("ssh", "mosh"):
            path = bindir / name
            path.write_text(fixture)
            path.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(bindir),
            "COLOR_TEST_CAPTURE": str(self.capture),
            "TERM_PROGRAM": "ghostty",
        }
        self.env.pop("REMOTE_AGENT_AGENT_SERVER_SSH", None)

    def invoke(self, args, colorterm, *, script=None):
        self.capture.unlink(missing_ok=True)
        env = dict(self.env)
        env.pop("COLORTERM", None)
        if colorterm is not None:
            env["COLORTERM"] = colorterm
        command = (
            [sys.executable, "-c", script]
            if script is not None
            else [sys.executable, str(ROOT / "remote-agent.py"), *args]
        )
        result = subprocess.run(
            command, env=env, text=True, capture_output=True, timeout=10, check=False
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = [json.loads(line) for line in self.capture.read_text().splitlines()]
        return result, records

    def test_start_cli_forwards_only_normalized_color_capability(self):
        for colorterm, expected in COLOR_CASES:
            with self.subTest(colorterm=colorterm):
                _, records = self.invoke(["start", "engine", "demo"], colorterm)
                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0]["request"],
                    {
                        "action": "start",
                        "name": "demo",
                        "repo": "engine",
                        "agent": "pi",
                        "colorterm": expected,
                    },
                )

    def test_query_start_enriches_copy_without_mutating_caller_request(self):
        request = {"action": "start", "name": "demo", "repo": "engine"}
        script = (
            "import json, runpy; "
            f"controller = runpy.run_path({str(ROOT / 'remote-agent.py')!r}); "
            f"request = {request!r}; "
            "controller['query']('agent-server', request); "
            "print(json.dumps(request))"
        )
        for colorterm, expected in COLOR_CASES:
            with self.subTest(colorterm=colorterm):
                result, records = self.invoke([], colorterm, script=script)
                self.assertEqual(json.loads(result.stdout), request)
                self.assertEqual(
                    records[0]["request"], {**request, "colorterm": expected}
                )

    def test_attach_forwards_capability_before_attach_pair_in_both_user_routes(self):
        for user in (None, "alice"):
            for colorterm, expected in COLOR_CASES:
                with self.subTest(user=user, colorterm=colorterm):
                    args = ["attach", "demo"]
                    if user is not None:
                        args += ["--user", user]
                    _, records = self.invoke(args, colorterm)
                    mosh = [r for r in records if r["transport"] == "mosh"]
                    self.assertEqual(len(mosh), 1)
                    destination = ["fixture-server", "python3"]
                    if user is None:
                        destination += [
                            "/home/fixture/Projects/cli/remote-agent/remote-agent-worker.py",
                            "--host",
                            "agent-server",
                        ]
                    else:
                        destination += [
                            "/home/fixture/Projects/cli/remote-agent/remote-agent-user.py",
                            user,
                            "agent-server",
                        ]
                    self.assertEqual(
                        mosh[0]["argv"],
                        [
                            "--no-init",
                            "--",
                            *destination,
                            f"--colorterm={expected}",
                            "--attach",
                            "demo",
                        ],
                    )
                    self.assertEqual(
                        [r["request"] for r in records if r["transport"] == "ssh"],
                        ([{"action": "list"}] if user else [])
                        + [{"action": "status", "name": "demo"}],
                    )

    def test_nonstart_cli_requests_do_not_gain_color_fields(self):
        for action in ("list", "status", "logs", "cancel"):
            with self.subTest(action=action):
                args = [action] if action == "list" else [action, "demo"]
                _, records = self.invoke(args, "truecolor")
                expected = {"action": action}
                if action != "list":
                    expected["name"] = "demo"
                self.assertEqual(records[0]["request"], expected)


if __name__ == "__main__":
    unittest.main()

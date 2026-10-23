"""Credential isolation at the container's Pi launch boundary."""

import importlib.util
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "sandbox" / "launch.py"


def load():
    spec = importlib.util.spec_from_file_location("sandbox_launch", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SandboxLaunchTests(unittest.TestCase):
    def test_only_selected_runtime_environment_and_openrouter_key_survive(self):
        environment = load().pi_environment(
            {
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "ANTHROPIC_API_KEY": "not-shared",
                "SSH_AUTH_SOCK": "/host/agent",
                "HOME": "/host/home",
                "LD_PRELOAD": "/host/evil.so",
            },
            "  sk-or-test-only\n",
        )
        self.assertEqual(environment["OPENROUTER_API_KEY"], "sk-or-test-only")
        self.assertEqual(environment["HOME"], "/home/agent")
        self.assertEqual(environment["COLORTERM"], "truecolor")
        self.assertEqual(environment["TD_NO_UPDATE"], "1")
        self.assertEqual(environment["PI_SKIP_VERSION_CHECK"], "1")
        self.assertEqual(environment["PI_OFFLINE"], "1")
        self.assertEqual(environment["PI_KB_DIR"], "/home/agent/kb")
        for key in ("ANTHROPIC_API_KEY", "SSH_AUTH_SOCK", "LD_PRELOAD"):
            self.assertNotIn(key, environment)

    def test_missing_or_multiline_key_fails_explicitly(self):
        for key in ("", " \n", "first\nsecond", "first second"):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "OpenRouter key"),
            ):
                load().pi_environment({}, key)

    def test_export_receives_only_supported_export_arguments(self):
        arguments = ["--export-terminal", "fixture-id", "--export-columns", "120"]
        self.assertEqual(load().pi_arguments(arguments), ["/opt/pi/pi", *arguments])

    def test_arguments_select_only_configured_openrouter_model(self):
        arguments = load().pi_arguments(["--session", "fixture-id"])
        self.assertEqual(arguments[-2:], ["--session", "fixture-id"])
        self.assertEqual(arguments[arguments.index("--provider") + 1], "openrouter")
        self.assertEqual(
            arguments[arguments.index("--model") + 1],
            "deepseek/deepseek-v4.1-flash-1017",
        )
        # First-party extensions (agents, goal, loop) load; settings.json disables host-only ones.
        self.assertNotIn("--no-extensions", arguments)
        self.assertIn("--no-context-files", arguments)


if __name__ == "__main__":
    unittest.main()

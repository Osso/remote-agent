"""Shared account ownership through local worker/controller subprocesses only."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_remote_agent import ROOT, write_config


class ClaudeAccountOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        write_config(self.home)
        self.legacy = self.home / ".claude"
        self.legacy.mkdir(mode=0o700)
        self.profiles = self.home / ".config/pi-claude-bridge/profiles"
        self.selection = self.legacy / ".active-profile"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.capture = self.home / "agent-capture.json"
        fixture = f"""#!{sys.executable}
import json, os, pathlib, sys
pathlib.Path({str(self.capture)!r}).write_text(json.dumps({{'env': dict(os.environ), 'argv': sys.argv[1:]}}))
if sys.argv[1:] == ['auth', 'login', '--claudeai']:
    if os.environ.get('FIXTURE_LOGIN_FAIL'):
        sys.exit(7)
    destination = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / '.credentials.json'
    destination.write_text(json.dumps({{'claudeAiOauth': {{'accessToken': 'fixture-direct-login'}}}}))
"""
        for name in ("claude", "pi"):
            executable = self.bin / name
            executable.write_text(fixture)
            executable.chmod(0o700)
        ssh = self.bin / "ssh"
        ssh.write_text(f"""#!{sys.executable}
import subprocess, sys
args = sys.argv[1:]
if args[0] == '-t':
    args = args[1:]
# Replace only the fixture destination with the real local worker.
sys.exit(subprocess.call([{sys.executable!r}, {str(ROOT / "remote-agent-worker.py")!r}, *args[3:]]))
""")
        ssh.chmod(0o700)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "CLAUDE_CONFIG_DIR": "/fixture/stale-config",
            "ANTHROPIC_API_KEY": "fixture-stale-api",
            "ANTHROPIC_AUTH_TOKEN": "fixture-stale-auth",
            "CLAUDE_CODE_OAUTH_TOKEN": "fixture-stale-oauth",
        }

    def credentials(self, token):
        return json.dumps({"claudeAiOauth": {"accessToken": token}}).encode()

    def credential_path(self, name):
        return self.profiles / name / ".credentials.json"

    def canonical(self, name, token):
        path = self.credential_path(name)
        path.parent.mkdir(parents=True, mode=0o700)
        (path.parent / ".credential-owner").write_text("shared-v1\n")
        if token is not None:
            path.write_bytes(self.credentials(token))
            path.chmod(0o600)
        return path

    def account(self, **request):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "remote-agent-worker.py"),
                "--host",
                "agent-server",
            ],
            input=json.dumps({"action": "claude-account", **request}),
            env=self.env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def controller(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "remote-agent.py"), "claude-account", *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def launch(self, agent):
        env = {
            **self.env,
            "REMOTE_AGENT_CONVERSATION": '["--session-id", "fixture-conversation"]',
        }
        code = (
            "import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('worker', {str(ROOT / 'remote-agent-worker.py')!r}); "
            "worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker); "
            f"worker.launch_agent({agent!r}, {str(self.bin / agent)!r}, {self.env['PATH']!r}, {str(self.home)!r}, None, tmux=False)"
        )
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def test_canonical_switch_never_copies_credentials(self):
        named = self.canonical("named", "fixture-canonical")
        other = self.canonical("other", "fixture-other")
        self.selection.write_text("named")
        global_path = self.legacy / ".credentials.json"
        global_path.write_bytes(self.credentials("fixture-stale-global"))
        legacy_named = self.legacy / "named.credentials.json"
        legacy_named.write_bytes(self.credentials("fixture-stale-named"))
        before = {
            p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
            for p in (named, other, global_path, legacy_named)
        }

        result = self.account(use="other")
        self.assert_success(result)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "active": "other",
                "unnamed_active": False,
                "profiles": ["named", "other"],
            },
        )
        for path, original in before.items():
            self.assertEqual(
                (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns),
                original,
            )

    def test_deleted_or_empty_owned_grant_is_not_restored(self):
        self.canonical("other", "fixture-other")
        for token in (None, "", "   "):
            with self.subTest(token=token):
                path = self.credential_path("named")
                if not path.parent.exists():
                    self.canonical("named", None)
                path.unlink(missing_ok=True)
                if token is not None:
                    path.write_bytes(self.credentials(token))
                self.selection.write_text("other")
                (self.legacy / ".credentials.json").write_bytes(
                    self.credentials("fixture-global")
                )
                (self.legacy / "named.credentials.json").write_bytes(
                    self.credentials("fixture-legacy")
                )
                result = self.account(use="named")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.selection.read_text(), "other")
                self.assertEqual(path.exists(), token is not None)
                if token is not None:
                    self.assertEqual(path.read_bytes(), self.credentials(token))

    def test_listing_uses_canonical_directories_not_legacy_files(self):
        self.canonical("other", "fixture-other")
        self.canonical("cleared", None)
        (self.legacy / "legacy.credentials.json").write_bytes(
            self.credentials("fixture-old")
        )
        result = self.account()
        self.assert_success(result)
        self.assertEqual(json.loads(result.stdout)["profiles"], ["cleared", "other"])

    def test_unnamed_global_import_moves_once_and_refuses_overwrite(self):
        global_path = self.legacy / ".credentials.json"
        global_path.write_bytes(self.credentials("fixture-unnamed"))
        self.assert_success(self.account(name_current="named"))
        path = self.credential_path("named")
        self.assertEqual(path.read_bytes(), self.credentials("fixture-unnamed"))
        self.assertFalse(global_path.exists())
        self.assertEqual((path.parent / ".credential-owner").read_text(), "shared-v1\n")
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.selection.unlink()
        path.unlink()
        global_path.write_bytes(self.credentials("fixture-new-unnamed"))
        self.assertNotEqual(self.account(name_current="named").returncode, 0)
        self.assertFalse(path.exists())
        self.assertEqual(
            global_path.read_bytes(), self.credentials("fixture-new-unnamed")
        )

    def test_import_rejects_empty_or_invalid_token_without_creating_owner(self):
        for token in (None, "", "  ", 123):
            with self.subTest(token=token):
                (self.legacy / ".credentials.json").write_bytes(self.credentials(token))
                self.assertNotEqual(self.account(name_current="named").returncode, 0)
                self.assertFalse((self.profiles / "named").exists())
                self.assertFalse(self.selection.exists())

    def test_invalid_missing_and_unprepared_profiles_preserve_selection(self):
        self.canonical("other", "fixture-other")
        self.selection.write_text("other")
        pending = self.profiles / "pending"
        pending.mkdir()
        for name in ("../other", 3, "missing", "pending"):
            with self.subTest(name=name):
                self.assertNotEqual(self.account(use=name).returncode, 0)
                self.assertEqual(self.selection.read_text(), "other")

    def test_release_request_cannot_unselect_account_onto_legacy_global(self):
        path = self.canonical("named", "fixture-selected")
        self.selection.write_text("named")
        global_path = self.legacy / ".credentials.json"
        global_path.write_bytes(self.credentials("fixture-stale-global"))
        self.account(release=True)
        self.assertEqual(self.selection.read_text(), "named")
        self.assertEqual(
            global_path.read_bytes(), self.credentials("fixture-stale-global")
        )
        self.assert_success(self.launch("claude"))
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["env"]["CLAUDE_CONFIG_DIR"], str(path.parent))

    def test_native_claude_launch_uses_selected_account_and_clears_stale_env(self):
        path = self.canonical("named", "fixture-selected")
        self.selection.write_text("named")
        result = self.launch("claude")
        self.assert_success(result)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["env"]["CLAUDE_CONFIG_DIR"], str(path.parent))
        self.assertEqual(capture["argv"], ["--session-id", "fixture-conversation"])
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            self.assertNotIn(name, capture["env"])

    def test_pi_launch_preserves_caller_credential_environment(self):
        result = self.launch("pi")
        self.assert_success(result)
        capture = json.loads(self.capture.read_text())
        for name in (
            "CLAUDE_CONFIG_DIR",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            self.assertEqual(capture["env"][name], self.env[name])

    def test_direct_login_selects_only_after_success_in_named_canonical(self):
        self.canonical("other", "fixture-other")
        self.selection.write_text("other")
        global_path = self.legacy / ".credentials.json"
        global_path.write_bytes(self.credentials("fixture-unrelated"))
        result = self.controller("--login", "named")
        self.assert_success(result)
        path = self.credential_path("named")
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["env"]["CLAUDE_CONFIG_DIR"], str(path.parent))
        self.assertEqual(path.read_bytes(), self.credentials("fixture-direct-login"))
        self.assertEqual(self.selection.read_text(), "named")
        self.assertEqual(
            global_path.read_bytes(), self.credentials("fixture-unrelated")
        )
        self.assertEqual((path.parent / ".credential-owner").read_text(), "shared-v1\n")
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            self.assertNotIn(name, capture["env"])

    def test_failed_login_preserves_old_selection_and_other_accounts(self):
        other = self.canonical("other", "fixture-other")
        self.selection.write_text("other")
        self.env["FIXTURE_LOGIN_FAIL"] = "1"
        result = self.controller("--login", "named")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(self.selection.read_text(), "other")
        self.assertEqual(other.read_bytes(), self.credentials("fixture-other"))
        self.assertFalse(self.credential_path("named").exists())
        self.assertEqual(
            (self.profiles / "named/.credential-owner").read_text(), "shared-v1\n"
        )

    def test_reauthentication_uses_existing_owned_directory(self):
        self.canonical("named", None)
        self.assert_success(self.controller("--login", "named"))
        self.assertEqual(
            self.credential_path("named").read_bytes(),
            self.credentials("fixture-direct-login"),
        )
        self.assertEqual(self.selection.read_text(), "named")

    def test_prepare_login_claims_unowned_directory_without_restoring_credentials(self):
        self.canonical("other", "fixture-other")
        self.selection.write_text("other")
        global_path = self.legacy / ".credentials.json"
        named_path = self.legacy / "named.credentials.json"
        global_path.write_bytes(self.credentials("fixture-stale-global"))
        named_path.write_bytes(self.credentials("fixture-stale-named"))
        before = {p: p.read_bytes() for p in (global_path, named_path)}
        for index, data in enumerate((None, b"", b"{}", b" \n")):
            with self.subTest(data=data):
                name = f"named{index}"
                path = self.credential_path(name)
                path.parent.mkdir()
                if data is not None:
                    path.write_bytes(data)
                # An explicit import still cannot claim an existing directory.
                self.selection.unlink()
                self.assertNotEqual(self.account(name_current=name).returncode, 0)
                self.selection.write_text("other")
                self.assert_success(self.account(prepare_login=name))
                self.assertEqual(path.exists(), data is not None)
                if data is not None:
                    self.assertEqual(path.read_bytes(), data)
                self.assertEqual(
                    (path.parent / ".credential-owner").read_text(), "shared-v1\n"
                )
                self.assertFalse((path.parent / ".credential-migration").exists())
                self.assertEqual(self.selection.read_text(), "other")
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_login_reauthenticates_unowned_empty_canonical_without_legacy_import(self):
        self.canonical("other", "fixture-other")
        self.selection.write_text("other")
        global_path = self.legacy / ".credentials.json"
        named_path = self.legacy / "named.credentials.json"
        global_path.write_bytes(self.credentials("fixture-stale-global"))
        named_path.write_bytes(self.credentials("fixture-stale-named"))
        for fail in (True, False):
            with self.subTest(fail=fail):
                path = self.credential_path("named")
                path.parent.mkdir(exist_ok=True)
                (path.parent / ".credential-owner").unlink(missing_ok=True)
                path.write_bytes(b"")
                if fail:
                    self.env["FIXTURE_LOGIN_FAIL"] = "1"
                else:
                    self.env.pop("FIXTURE_LOGIN_FAIL")
                result = self.controller("--login", "named")
                self.assertEqual(result.returncode, 7 if fail else 0, result.stderr)
                capture = json.loads(self.capture.read_text())
                self.assertEqual(capture["env"]["CLAUDE_CONFIG_DIR"], str(path.parent))
                self.assertEqual(
                    path.read_bytes(),
                    b"" if fail else self.credentials("fixture-direct-login"),
                )
                self.assertEqual(
                    self.selection.read_text(), "other" if fail else "named"
                )
        self.assertEqual(
            global_path.read_bytes(), self.credentials("fixture-stale-global")
        )
        self.assertEqual(
            named_path.read_bytes(), self.credentials("fixture-stale-named")
        )

    def test_login_rejects_symlink_directory_without_claiming_target(self):
        target = self.home / "unowned"
        target.mkdir()
        self.profiles.mkdir(parents=True)
        (self.profiles / "named").symlink_to(target, target_is_directory=True)
        self.assertNotEqual(self.account(prepare_login="named").returncode, 0)
        self.assertFalse((target / ".credential-owner").exists())
        self.assertFalse((target / ".credential-migration").exists())

    def test_preexisting_migration_lock_blocks_login_and_import(self):
        path = self.canonical("named", None).parent
        lock = path / ".credential-migration"
        lock.mkdir()
        (self.legacy / ".credentials.json").write_bytes(
            self.credentials("fixture-global")
        )
        self.assertNotEqual(self.account(prepare_login="named").returncode, 0)
        self.assertNotEqual(self.account(name_current="named").returncode, 0)
        self.assertTrue(lock.is_dir())
        self.assertFalse(self.credential_path("named").exists())
        self.assertFalse(self.selection.exists())

    def test_native_launch_without_usable_selected_grant_does_not_fall_back(self):
        for selected in (None, "cleared"):
            with self.subTest(selected=selected):
                if selected:
                    self.canonical(selected, None)
                    self.selection.write_text(selected)
                result = self.launch("claude")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_login_validation_stops_before_authentication(self):
        self.canonical("other", "fixture-other")
        self.selection.write_text("other")
        for name in ("../bad", "pending"):
            with self.subTest(name=name):
                if name == "pending":
                    directory = self.profiles / name
                    directory.mkdir()
                    (directory / ".credential-owner").write_text("invalid-owner\n")
                result = self.controller("--login", name)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())
                self.assertEqual(self.selection.read_text(), "other")


if __name__ == "__main__":
    unittest.main()

"""Behavioral UNC routing tests with isolated host configuration."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = {"sample": {"remote": "Projects/sample", "local": "/fixture/sample"}}


class UncMappingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def load_controller(self, distro="FixtureLinux", desktop=True, wrap=None):
        hosts = {
            "agent-server": {
                "ssh": "fixture-server",
                "home": "/home/fixture-server",
                "wrap": [],
                "windows_shell": False,
            }
        }
        if desktop:
            hosts["desktop"] = {
                "ssh": "fixture-desktop",
                "home": "/home/fixture-desktop",
                "wrap": (["wsl.exe", "-d", distro, "--exec"] if wrap is None else wrap),
                "windows_shell": True,
            }
        config = self.home / ".config/remote-agent"
        config.mkdir(parents=True, exist_ok=True)
        (config / "remote-hosts.json").write_text(json.dumps(hosts))
        (config / "remote-projects.json").write_text(json.dumps(PROJECTS))
        spec = importlib.util.spec_from_file_location(
            "unc_test_controller", ROOT / "remote-agent.py"
        )
        controller = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(controller)
        return controller

    def test_unc_variants_use_configured_distribution(self):
        for distro in ("FixtureLinux", "AnotherLinux"):
            controller = self.load_controller(distro=distro)
            for server in ("wsl$", "wsl.localhost"):
                with self.subTest(distro=distro, server=server):
                    path = f"\\\\{server}\\{distro}\\home\\fixture\\image one.png"
                    self.assertEqual(
                        controller.fetch_target(path, "sample", PROJECTS, "desktop"),
                        ("desktop", "/home/fixture/image one.png"),
                    )

    def test_unc_matching_remains_case_insensitive(self):
        controller = self.load_controller()
        for server in ("WSL$", "WSL.LOCALHOST"):
            with self.subTest(server=server):
                self.assertEqual(
                    controller.fetch_target(
                        f"\\\\{server}\\fixturelinux\\Home\\Image.PNG",
                        "sample",
                        PROJECTS,
                        "desktop",
                    ),
                    ("desktop", "/Home/Image.PNG"),
                )

    def test_unc_distribution_root_is_absolute(self):
        controller = self.load_controller()
        self.assertEqual(
            controller.fetch_target(
                "\\\\wsl$\\FixtureLinux\\", "sample", PROJECTS, "desktop"
            ),
            ("desktop", "/"),
        )

    def test_unc_rejects_selected_native_host(self):
        controller = self.load_controller()
        for server in ("wsl$", "wsl.localhost"):
            with (
                self.subTest(server=server),
                self.assertRaisesRegex(ValueError, "UNC paths require desktop"),
            ):
                controller.fetch_target(
                    f"\\\\{server}\\FixtureLinux\\tmp\\image.png",
                    "sample",
                    PROJECTS,
                )

    def test_unknown_distribution_is_not_a_project_relative_path(self):
        controller = self.load_controller()
        for server in ("wsl$", "wsl.localhost"):
            with (
                self.subTest(server=server),
                self.assertRaisesRegex(ValueError, "unknown WSL distribution.*Other"),
            ):
                controller.fetch_target(
                    f"\\\\{server}\\Other\\tmp\\image.png",
                    "sample",
                    PROJECTS,
                    "desktop",
                )

    def test_native_paths_work_without_desktop_profile(self):
        controller = self.load_controller(desktop=False)
        for path, expected in (
            ("/tmp/image one.png", "/tmp/image one.png"),
            ("~/image.png", "/home/fixture-server/image.png"),
            (
                "assets/image.png",
                "/home/fixture-server/Projects/sample/assets/image.png",
            ),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    controller.fetch_target(path, "sample", PROJECTS),
                    ("agent-server", expected),
                )

    def test_unc_without_desktop_profile_fails_explicitly(self):
        controller = self.load_controller(desktop=False)
        with self.assertRaisesRegex(ValueError, "desktop host profile"):
            controller.fetch_target(
                "\\\\wsl$\\FixtureLinux\\tmp\\image.png", "sample", PROJECTS, "desktop"
            )

    def test_unc_without_configured_distribution_fails_explicitly(self):
        for wrap in (["wsl.exe", "--exec"], ["wsl.exe", "-d"]):
            controller = self.load_controller(wrap=wrap)
            with (
                self.subTest(wrap=wrap),
                self.assertRaisesRegex(ValueError, "desktop.*distribution"),
            ):
                controller.fetch_target(
                    "\\\\wsl$\\FixtureLinux\\tmp\\image.png",
                    "sample",
                    PROJECTS,
                    "desktop",
                )


if __name__ == "__main__":
    unittest.main()

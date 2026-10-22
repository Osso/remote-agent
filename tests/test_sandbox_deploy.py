"""Observed host-route input at the sandbox deployment boundary."""

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / "sandbox"


def load():
    spec = importlib.util.spec_from_file_location(
        "sandbox_host_deploy", DIRECTORY / "host_deploy.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(DIRECTORY), *sys.path]):
        spec.loader.exec_module(module)
    return module


class SandboxDeploymentTests(unittest.TestCase):
    def test_linux_host_routes_are_preserved_as_single_address_blocks(self):
        routes = [
            {"dst": "192.168.2.1"},
            {"dst": "192.168.2.0/24"},
            {"dst": "172.17.0.0/16"},
            {"dst": "192.168.2.1"},
            {"dst": "default"},
        ]
        self.assertEqual(
            load().denied_routes(routes),
            ["172.17.0.0/16", "192.168.2.0/24", "192.168.2.1/32"],
        )

    def test_invalid_destination_fails_before_firewall_install(self):
        for destination in ("not-a-route", "::1", "192.168.2.1; accept"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                load().denied_routes([{"dst": destination}])


if __name__ == "__main__":
    unittest.main()

"""External nft batch and command-line contracts for the sandbox firewall."""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIREWALL = ROOT / "sandbox" / "firewall.py"

DEFAULT_POLICY = """destroy table inet clank_sandbox
table inet clank_sandbox {
    chain input {
        type filter hook input priority -10; policy accept;
        iifname "br-clank" counter drop
    }
    chain forward {
        type filter hook forward priority -10; policy accept;
        iifname "br-clank" meta nfproto ipv6 counter drop
        iifname "br-clank" ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/4, 240.0.0.0/4 } counter drop
        iifname "br-clank" fib daddr type local counter drop
        oifname "br-clank" ct state != { established, related } counter drop
    }
}
"""


class SandboxFirewallTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(FIREWALL.is_file(), "sandbox firewall renderer is missing")
        spec = importlib.util.spec_from_file_location("sandbox_firewall", FIREWALL)
        self.firewall = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.firewall)

    def test_default_atomic_batch_contract(self):
        self.assertEqual(self.firewall.render_policy(), DEFAULT_POLICY)

    def test_custom_bridge_and_overlapping_routes(self):
        expected = DEFAULT_POLICY.replace("br-clank", "sandbox.42")
        extra = (
            '        iifname "sandbox.42" ip daddr 10.0.0.0/8 counter drop\n'
            '        iifname "sandbox.42" ip daddr 8.8.8.0/24 counter drop\n'
            '        iifname "sandbox.42" ip daddr 8.8.8.8/32 counter drop\n'
        )
        marker = '        iifname "sandbox.42" fib daddr type local counter drop\n'
        self.assertEqual(
            self.firewall.render_policy(
                bridge="sandbox.42",
                extra_ipv4=("10.0.0.0/8", "8.8.8.0/24", "8.8.8.8/32"),
            ),
            expected.replace(marker, extra + marker),
        )

    def test_route_iterable_and_repeated_calls_are_independent(self):
        routes = (route for route in ("0.0.0.0/0", "8.8.8.8/32"))
        policy = self.firewall.render_policy(extra_ipv4=routes)
        self.assertIn('iifname "br-clank" ip daddr 0.0.0.0/0 counter drop\n', policy)
        self.assertIn('iifname "br-clank" ip daddr 8.8.8.8/32 counter drop\n', policy)
        self.assertEqual(self.firewall.render_policy(), DEFAULT_POLICY)

    def test_bridge_accepts_safe_ascii_names_up_to_fifteen_bytes(self):
        for bridge in ("b", "123456789012345", "br_test-1.2", "-sandbox"):
            with self.subTest(bridge=bridge):
                self.assertEqual(
                    self.firewall.render_policy(bridge),
                    DEFAULT_POLICY.replace("br-clank", bridge),
                )

    def test_bridge_rejects_unsafe_or_invalid_interface_names(self):
        for bridge in (
            "",
            ".",
            "..",
            "1234567890123456",
            "br/evil",
            "br:evil",
            "br evil",
            'br"; drop',
            "br\nevil",
            "br\x00evil",
            "br*",
            "bré",
            None,
            123,
        ):
            with self.subTest(bridge=bridge):
                with self.assertRaisesRegex(ValueError, "bridge"):
                    self.firewall.render_policy(bridge=bridge)

    def test_routes_reject_noncanonical_non_ipv4_and_injection(self):
        for route in (
            "8.8.8.1/24",
            "8.8.8.8",
            "8.8.8.0/255.255.255.0",
            "8.8.8.0/024",
            "::/0",
            "::ffff:8.8.8.8/128",
            "256.0.0.0/8",
            "10.0.0.0/33",
            " 8.8.8.0/24",
            "8.8.8.0/24\n",
            "8.8.8.0/24; flush ruleset",
            "",
            None,
            123,
        ):
            with self.subTest(route=route):
                with self.assertRaisesRegex(ValueError, "IPv4.*CIDR"):
                    self.firewall.render_policy(extra_ipv4=(route,))

    def test_cli_prints_default_batch_only(self):
        result = subprocess.run(
            [sys.executable, str(FIREWALL)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, DEFAULT_POLICY)
        self.assertEqual(result.stderr, "")

    def test_cli_accepts_repeated_deny_routes(self):
        result = subprocess.run(
            [
                sys.executable,
                str(FIREWALL),
                "--bridge",
                "br-test",
                "--deny-route",
                "8.8.8.0/24",
                "--deny-route",
                "1.1.1.1/32",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            self.firewall.render_policy("br-test", ("8.8.8.0/24", "1.1.1.1/32")),
        )
        self.assertEqual(result.stderr, "")

    def test_cli_invalid_input_prints_no_partial_batch(self):
        for arguments in (
            ("--bridge", 'br"; drop'),
            ("--deny-route", "10.0.0.1/8"),
            ("--deny-route", "8.8.8.0/24", "--deny-route", "::/0"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(FIREWALL), *arguments],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()

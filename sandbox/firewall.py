#!/usr/bin/env python3
"""Render an atomic, sandbox-only nftables batch; never apply host changes."""

import argparse
import ipaddress
import re
from collections.abc import Iterable

DENIED_IPV4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def validate_route(route: str) -> str:
    """Reject non-IPv4, noncanonical CIDRs before interpolating nft text."""
    message = f"expected canonical IPv4 network CIDR, got {route!r}"
    if not isinstance(route, str):
        raise ValueError(message)
    try:
        network = ipaddress.IPv4Network(route, strict=True)
    except ValueError as error:
        raise ValueError(message) from error
    if str(network) != route:
        raise ValueError(message)
    return route


def render_policy(bridge: str = "br-clank", extra_ipv4: Iterable[str] = ()) -> str:
    """Drop sandbox host/private traffic without accepting past Docker chains.

    Submit the entire returned batch in one nft invocation for atomic replacement.
    Bridge names use a conservative ASCII subset of Linux's 15-byte limit.
    Extra routes have individual rules so overlapping networks remain valid.
    """
    if (
        not isinstance(bridge, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", bridge) is None
        or bridge in (".", "..")
    ):
        raise ValueError("bridge must be a safe Linux interface name (1-15 bytes)")
    routes = tuple(validate_route(route) for route in extra_ipv4)
    extra_rules = "".join(
        f'        iifname "{bridge}" ip daddr {route} counter drop\n'
        for route in routes
    )
    denied = ", ".join(DENIED_IPV4)
    return f"""destroy table inet clank_sandbox
table inet clank_sandbox {{
    chain input {{
        type filter hook input priority -10; policy accept;
        iifname "{bridge}" counter drop
    }}
    chain forward {{
        type filter hook forward priority -10; policy accept;
        iifname "{bridge}" meta nfproto ipv6 counter drop
        iifname "{bridge}" ip daddr {{ {denied} }} counter drop
{extra_rules}        iifname "{bridge}" fib daddr type local counter drop
        oifname "{bridge}" ct state != {{ established, related }} counter drop
    }}
}}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", default="br-clank")
    parser.add_argument("--deny-route", action="append", default=[], metavar="CIDR")
    arguments = parser.parse_args()
    try:
        policy = render_policy(arguments.bridge, arguments.deny_route)
    except ValueError as error:
        parser.error(str(error))
    print(policy, end="")


if __name__ == "__main__":
    main()

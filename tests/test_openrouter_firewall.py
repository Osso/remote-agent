import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "sandbox/openrouter-runtime"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_firewall_policy_is_default_deny_for_bridge():
    policy = load("host_deploy").policy()
    assert (
        policy
        == """destroy table inet openrouter_sandbox
table inet openrouter_sandbox {
 chain input {
  type filter hook input priority -10; policy accept;
  iifname "br-or-sandbox" meta nfproto ipv4 ip daddr 172.30.253.1 tcp dport 3128 accept
  iifname "br-or-sandbox" drop
 }
 chain forward {
  type filter hook forward priority -10; policy accept;
  iifname "br-or-sandbox" drop
  oifname "br-or-sandbox" drop
 }
}
"""
    )


def test_container_uses_loopback_dns_and_private_mounts():
    session = load("session")
    config = {
        "home_volume": "openrouter-pi-home",
        "workspace_volume": "openrouter-pi-workspace",
        "secret_file": "/private/key",
    }
    options = session.container_options(config, "openrouter-sandbox", False)
    assert options[options.index("--dns") + 1] == "127.0.0.1"
    assert "net.ipv6.conf.all.disable_ipv6=1" in options
    assert (
        "type=bind,src=/private/key,dst=/run/secrets/openrouter.key,readonly" in options
    )

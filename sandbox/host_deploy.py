#!/usr/bin/env python3
"""Install the dedicated sandbox on agent-server; invoked by deploy.py."""

import hashlib
import ipaddress
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from firewall import render_policy

ROOT = Path.home() / ".local/share/clank-sandbox"
NETWORK = "clank-sandbox"
SERVICE = "clank-sandbox-firewall.service"


def command(*arguments, **options):
    return subprocess.run(arguments, check=True, **options)


def capture(*arguments):
    return command(*arguments, text=True, capture_output=True).stdout.strip()


def pull(image):
    for attempt in range(3):
        result = subprocess.run(["docker", "pull", image], check=False)
        if result.returncode == 0:
            return
        if attempt == 2:
            result.check_returncode()
        time.sleep(2**attempt + __import__("random").random())


def denied_routes(routes):
    return sorted(
        {
            str(ipaddress.IPv4Network(route["dst"], strict=False))
            for route in routes
            if route.get("dst") != "default"
        }
    )


def install_firewall(stage):
    routes = json.loads(capture("ip", "-j", "-4", "route", "show", "scope", "link"))
    denied = denied_routes(routes)
    policy = stage / "clank-sandbox.nft"
    policy.write_text(render_policy(extra_ipv4=denied))
    unit = stage / SERVICE
    unit.write_text(
        "[Unit]\nDescription=Clank sandbox network boundary\nBefore=docker.service\n"
        "[Service]\nType=oneshot\nExecStart=/usr/bin/nft -f /etc/clank-sandbox.nft\n"
        "RemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n"
    )
    command("authsudo", "nft", "--check", "--file", str(policy))
    for source, destination in (
        (policy, "/etc/clank-sandbox.nft"),
        (unit, f"/etc/systemd/system/{SERVICE}"),
    ):
        command("authsudo", "install", "-m", "0644", str(source), destination)
    command("authsudo", "systemctl", "daemon-reload")
    command("authsudo", "systemctl", "enable", SERVICE)
    # Only this owned policy service is restarted; Docker and existing jobs stay up.
    command("authsudo", "systemctl", "restart", SERVICE)


def ensure_network():
    found = subprocess.run(
        ["docker", "network", "inspect", NETWORK],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode == 0:
        config = json.loads(found.stdout)[0]
        options = config.get("Options", {})
        if (
            config["Driver"] != "bridge"
            or config.get("EnableIPv6")
            or options.get("com.docker.network.bridge.name") != "br-clank"
            or options.get("com.docker.network.bridge.enable_icc") != "false"
        ):
            raise RuntimeError(
                "existing clank-sandbox network has incompatible isolation; not modifying it"
            )
        return
    command(
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--opt",
        "com.docker.network.bridge.name=br-clank",
        "--opt",
        "com.docker.network.bridge.enable_icc=false",
        NETWORK,
    )


def build_td(stage):
    builder = "node:22.21.0-bookworm-slim"
    pull(builder)
    command(
        "docker",
        "run",
        "--rm",
        "--user",
        "1000:1000",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--pids-limit",
        "256",
        "--memory",
        "2g",
        "--cpus",
        "2",
        "--network",
        NETWORK,
        "--dns",
        "1.1.1.1",
        "--dns",
        "8.8.8.8",
        "--sysctl",
        "net.ipv6.conf.all.disable_ipv6=1",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=1g",
        "--env",
        "HOME=/tmp",
        "--mount",
        f"type=bind,src={stage / 'clank'},dst=/build",
        "--workdir",
        "/build",
        builder,
        "node",
        "tools/build-td/build.mjs",
    )
    return capture("docker", "image", "inspect", "--format", "{{.Id}}", builder)


def build_runtime(stage):
    context = stage / "runtime"
    context.mkdir()
    for name in ("Dockerfile", "launch.py", "settings.json", "models.json"):
        shutil.copy2(stage / name, context / name)
    shutil.move(str(stage / "pi"), context / "pi")
    shutil.copy2(stage / "clank/dist/td-linux-x64", context / "td")
    shutil.copytree(
        stage / "clank", context / "clank", ignore=shutil.ignore_patterns("dist")
    )
    td_hash = hashlib.sha256((context / "td").read_bytes()).hexdigest()
    pi_hash = hashlib.sha256((context / "pi/pi").read_bytes()).hexdigest()
    tag = f"clank-pi:{td_hash[:16]}-{pi_hash[:12]}"
    command("docker", "build", "--pull", "--tag", tag, str(context))
    image = capture("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    return image, td_hash, pi_hash


def install_runtime(stage, image):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    for name in ("session.py", "launch.py", "firewall.py"):
        shutil.copy2(stage / name, ROOT / name)
        (ROOT / name).chmod(0o700)
    cancel = ROOT / "cancel.py"
    cancel.write_text(
        "#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\nos.execv(sys.executable, [sys.executable, str(Path(__file__).with_name('session.py')), '--cancel', *sys.argv[1:]])\n"
    )
    cancel.chmod(0o700)
    for volume in ("clank-pi-home", "clank-pi-workspace"):
        command("docker", "volume", "create", volume)
    config = {
        "image": image,
        "network": NETWORK,
        "home_volume": "clank-pi-home",
        "workspace_volume": "clank-pi-workspace",
        "secret_file": str(ROOT / "openrouter.key"),
        "firewall_service": SERVICE,
    }
    (ROOT / "runtime.json").write_text(json.dumps(config, indent=2) + "\n")
    (ROOT / "runtime.json").chmod(0o600)
    # The host project directory is only the worker's cwd; it is never mounted.
    cwd = Path.home() / "Projects/clank-sandbox"
    cwd.mkdir(parents=True, exist_ok=True)
    mapping = Path.home() / ".config/remote-agent/remote-projects.json"
    projects = json.loads(mapping.read_text())
    entry = {
        "local": "/home/osso/Repos/clank-together",
        "remote": "Projects/clank-sandbox",
        "pi_executable": str(ROOT / "session.py"),
        "pi_cancel_executable": str(cancel),
    }
    if "clank" in projects and projects["clank"] != entry:
        raise RuntimeError("existing clank mapping differs; refusing to overwrite it")
    projects["clank"] = entry
    temporary = mapping.with_suffix(".json.new")
    temporary.write_text(json.dumps(projects, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(mapping)


def main():
    stage = Path(sys.argv[1]).resolve(strict=True)
    if stage.parent != ROOT or not stage.name.startswith("stage-"):
        raise ValueError("deployment stage must be a dedicated clank-sandbox child")
    install_firewall(stage)
    ensure_network()
    builder = build_td(stage)
    image, td_hash, pi_hash = build_runtime(stage)
    install_runtime(stage, image)
    proof = json.loads((stage / "source-manifest.json").read_text())
    proof.update(
        image=image, builder_image=builder, td_sha256=td_hash, pi_sha256=pi_hash
    )
    (ROOT / "deployment.json").write_text(json.dumps(proof, indent=2) + "\n")
    (ROOT / "deployment.json").chmod(0o600)
    print(json.dumps(proof, indent=2), flush=True)


if __name__ == "__main__":
    main()

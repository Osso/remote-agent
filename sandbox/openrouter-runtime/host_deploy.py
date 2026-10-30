#!/usr/bin/env python3
"""Install the independent OpenRouter runtime on the target host."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path.home() / ".local/share/openrouter-sandbox"
BRIDGE = "br-or-sandbox"
ADDRESS = "172.30.253.1"
NETWORK = "openrouter-sandbox"
FIREWALL = "openrouter-sandbox-firewall.service"
PROXY = "openrouter-sandbox-proxy.service"


def run(*args):
    return subprocess.run(
        args, check=True, text=True, capture_output=True
    ).stdout.strip()


def policy():
    return f'''destroy table inet openrouter_sandbox
table inet openrouter_sandbox {{
 chain input {{
  type filter hook input priority -10; policy accept;
  iifname "{BRIDGE}" meta nfproto ipv4 ip daddr {ADDRESS} tcp dport 3128 accept
  iifname "{BRIDGE}" drop
 }}
 chain forward {{
  type filter hook forward priority -10; policy accept;
  iifname "{BRIDGE}" drop
  oifname "{BRIDGE}" drop
 }}
}}
'''


def mapping(path, local):
    projects = json.loads(path.read_text()) if path.exists() else {}
    entry = {
        "local": local,
        "remote": "Projects/openrouter-sandbox",
        "pi_executable": str(ROOT / "session.py"),
        "pi_cancel_executable": str(ROOT / "cancel.py"),
    }
    if "openrouter-sandbox" in projects and projects["openrouter-sandbox"] != entry:
        raise ValueError("existing openrouter-sandbox project mapping differs")
    projects["openrouter-sandbox"] = entry
    return projects


def save_mapping(path, projects):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.new")
    temp.write_text(json.dumps(projects, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def install_configuration(stage, update_live_models=False):
    runtime = ROOT / "runtime.json"
    config = json.loads(runtime.read_text())
    image = run("docker", "image", "inspect", "--format", "{{.Id}}", config["image"])
    base = "openrouter-sandbox-config-base:latest"
    run("docker", "image", "tag", image, base)
    dockerfile = stage / "Dockerfile.config"
    dockerfile.write_text(
        f"FROM {base}\nCOPY settings.json models.json /opt/sandbox/config/\n"
    )
    image_id = stage / "image.id"
    run(
        "docker",
        "build",
        "--file",
        str(dockerfile),
        "--iidfile",
        str(image_id),
        str(stage),
    )
    config["image"] = image_id.read_text().strip()
    if update_live_models:
        script = (
            "import os,pathlib,tempfile; "
            "d=pathlib.Path('/home/agent/.config/pi/agent'); "
            "data=pathlib.Path('/opt/sandbox/config/models.json').read_bytes(); "
            "f=tempfile.NamedTemporaryFile(dir=d,delete=False); "
            "f.write(data); f.close(); os.replace(f.name,d/'models.json')"
        )
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "1000:1000",
            "--mount",
            f"type=volume,src={config['home_volume']},dst=/home/agent",
            "--entrypoint",
            "/usr/bin/python3",
            config["image"],
            "-c",
            script,
        )
    with tempfile.NamedTemporaryFile(mode="w", dir=ROOT, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(config, file, indent=2)
            file.write("\n")
            file.close()
            temporary.replace(runtime)
        finally:
            temporary.unlink(missing_ok=True)


def install(stage, local):
    if os.getuid() != 1000:
        raise ValueError("target user must have UID 1000 for the private key mount")
    mapping_path = Path.home() / ".config/remote-agent/remote-projects.json"
    projects = mapping(mapping_path, local)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    nft = stage / "openrouter-sandbox.nft"
    nft.write_text(policy())
    firewall = stage / FIREWALL
    firewall.write_text(
        "[Unit]\nDescription=OpenRouter sandbox firewall\nBefore=docker.service\n[Service]\nType=oneshot\nExecStart=/usr/bin/nft -f /etc/openrouter-sandbox.nft\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n"
    )
    proxy = stage / PROXY
    proxy.write_text(
        f"[Unit]\nDescription=OpenRouter sandbox proxy\nRequires=docker.service {FIREWALL}\nAfter=docker.service {FIREWALL}\n[Service]\nExecStart=/usr/bin/python3 {ROOT}/openrouter_proxy.py --host {ADDRESS} --port 3128\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n"
    )
    run("authsudo", "nft", "--check", "--file", str(nft))
    for src, dst in (
        (nft, "/etc/openrouter-sandbox.nft"),
        (firewall, "/etc/systemd/system/" + FIREWALL),
        (proxy, "/etc/systemd/system/" + PROXY),
    ):
        run("authsudo", "install", "-m", "0644", str(src), dst)
    run("authsudo", "systemctl", "daemon-reload")
    run("authsudo", "systemctl", "enable", FIREWALL, PROXY)
    run("authsudo", "systemctl", "restart", FIREWALL)
    result = subprocess.run(
        ["docker", "network", "inspect", NETWORK], capture_output=True, text=True
    )
    if result.returncode == 0:
        n = json.loads(result.stdout)[0]
        opts = n["Options"]
        if (
            n["Driver"] != "bridge"
            or not n["Internal"]
            or n["EnableIPv6"]
            or opts.get("com.docker.network.bridge.name") != BRIDGE
            or opts.get("com.docker.network.bridge.enable_icc") != "false"
            or n["IPAM"]["Config"]
            != [{"Subnet": "172.30.253.0/24", "Gateway": ADDRESS}]
        ):
            raise ValueError(
                "existing OpenRouter network differs; refusing to change it"
            )
    else:
        run(
            "docker",
            "network",
            "create",
            "--internal",
            "--subnet",
            "172.30.253.0/24",
            "--gateway",
            ADDRESS,
            "--opt",
            "com.docker.network.bridge.name=" + BRIDGE,
            "--opt",
            "com.docker.network.bridge.enable_icc=false",
            NETWORK,
        )
    for name in ("session.py", "openrouter_proxy.py"):
        shutil.copy2(stage / name, ROOT / name)
        (ROOT / name).chmod(0o700)
    cancel = ROOT / "cancel.py"
    cancel.write_text(
        "#!/usr/bin/python3\nimport os, sys\nfrom pathlib import Path\nos.execv(sys.executable, [sys.executable, str(Path(__file__).with_name('session.py')), '--cancel', *sys.argv[1:]])\n"
    )
    cancel.chmod(0o700)
    run("authsudo", "systemctl", "restart", PROXY)
    run(
        "docker", "build", "--pull", "--tag", "openrouter-sandbox-pi:latest", str(stage)
    )
    image = run(
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "openrouter-sandbox-pi:latest",
    )
    for vol in ("openrouter-pi-home", "openrouter-pi-workspace"):
        run("docker", "volume", "create", vol)
    config = {
        "image": image,
        "network": NETWORK,
        "home_volume": "openrouter-pi-home",
        "workspace_volume": "openrouter-pi-workspace",
        "secret_file": str(ROOT / "openrouter.key"),
        "firewall_service": PROXY,
    }
    (ROOT / "runtime.json").write_text(json.dumps(config, indent=2) + "\n")
    (ROOT / "runtime.json").chmod(0o600)
    (Path.home() / "Projects/openrouter-sandbox").mkdir(parents=True, exist_ok=True)
    save_mapping(mapping_path, projects)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("local", nargs="?")
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--update-live-models", action="store_true")
    args = parser.parse_args()
    if args.config_only:
        if args.local:
            parser.error("configuration-only deployment does not accept a project")
        install_configuration(args.stage.resolve(strict=True), args.update_live_models)
    else:
        if not args.local or args.update_live_models:
            parser.error(
                "full deployment requires a project and cannot update live models"
            )
        install(args.stage.resolve(strict=True), args.local)

#!/usr/bin/env python3
"""Rebuild only the owned launcher layer, preserving audited Pi and td bytes."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path.home() / ".local/share/clank-sandbox"
config = json.loads((root / "runtime.json").read_text())
launcher = Path(sys.argv[1])
revision = sys.argv[2]
digest = hashlib.sha256(launcher.read_bytes()).hexdigest()
base = "clank-pi:launcher-base"
tag = "clank-pi:launcher-" + digest[:16]
subprocess.run(["docker", "image", "tag", config["image"], base], check=True)
with tempfile.TemporaryDirectory(prefix="launcher-", dir=root) as temporary:
    context = Path(temporary)
    (context / "launch.py").write_bytes(launcher.read_bytes())
    (context / "Dockerfile").write_text(
        f"FROM {base}\nCOPY launch.py /opt/sandbox/launch.py\n"
    )
    subprocess.run(
        ["docker", "build", "--network", "none", "--tag", tag, str(context)], check=True
    )
image = subprocess.check_output(
    ["docker", "image", "inspect", "--format", "{{.Id}}", tag], text=True
).strip()
config["image"] = image
(root / "runtime.json").write_text(json.dumps(config, indent=2) + "\n")
proof = json.loads((root / "deployment.json").read_text())
proof.update(image=image, launcher_sha256=digest, launcher_revision=revision)
(root / "deployment.json").write_text(json.dumps(proof, indent=2) + "\n")
print("Launcher layer updated; td/Pi bytes and credentials unchanged.")

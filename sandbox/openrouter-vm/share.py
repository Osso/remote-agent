#!/usr/bin/env python3
"""Configure the fixed read-only World of Warcraft guest share."""

import os
import stat
import tempfile
from pathlib import Path

SOURCE = "wow-install"
TARGET = "/syncthing/World of Warcraft"
ESCAPED_TARGET = r"/syncthing/World\040of\040Warcraft"
ENTRY = f"{SOURCE} {ESCAPED_TARGET} 9p ro,trans=virtio,version=9p2000.L,cache=none,nodev,nosuid 0 0"


def atomic_write(path, content, mode, owner=None):
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            if owner is not None:
                os.fchown(output.fileno(), *owner)
            os.fchmod(output.fileno(), mode)
            output.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def configure(root=Path("/")):
    fstab = root / "etc/fstab"
    content = fstab.read_text()
    matches = 0
    for line in content.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 2:
            continue
        source, target = fields[:2]
        decoded_target = (
            target.replace(r"\040", " ").replace(r"\011", "\t").replace(r"\134", "\\")
        )
        if source == SOURCE or decoded_target == TARGET:
            if fields != ENTRY.split():
                raise ValueError(f"conflicting fstab entry: {line}")
            matches += 1
    if matches > 1:
        raise ValueError("duplicate wow-install fstab entries")

    target = root / TARGET.lstrip("/")
    if not target.is_dir():
        target.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(target, 0o755)
    modules = root / "etc/modules-load.d"
    modules.mkdir(parents=True, exist_ok=True, mode=0o755)
    atomic_write(modules / "wow-install.conf", "9p\n9pnet_virtio\n", 0o644)
    if not matches:
        info = fstab.stat()
        separator = "" if not content or content.endswith("\n") else "\n"
        atomic_write(
            fstab,
            content + separator + ENTRY + "\n",
            stat.S_IMODE(info.st_mode),
            (info.st_uid, info.st_gid),
        )
    print("configured, VM restart required for first activation")


if __name__ == "__main__":
    configure()

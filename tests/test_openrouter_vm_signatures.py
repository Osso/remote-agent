import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "sandbox/openrouter-vm/host_deploy.py"


@pytest.mark.skipif(
    not shutil.which("gpg"), reason="GnuPG is required for signature proof"
)
def test_official_armored_keyring_verifies_image_and_rejects_foreign_signature(
    tmp_path,
):
    spec = importlib.util.spec_from_file_location("vm_signatures", SOURCE)
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    keyhome = tmp_path / "gnupg"
    keyhome.mkdir(mode=0o700)
    command = [
        "gpg",
        "--homedir",
        str(keyhome),
        "--batch",
        "--pinentry-mode",
        "loopback",
        "--passphrase",
        "",
    ]
    subprocess.run(
        [
            *command,
            "--quick-generate-key",
            "Fixture <fixture@example.invalid>",
            "ed25519",
            "sign",
            "0",
        ],
        check=True,
        capture_output=True,
    )
    keyring = tmp_path / "archlinux.gpg"
    with keyring.open("wb") as output:
        subprocess.run([*command, "--armor", "--export"], stdout=output, check=True)
    image = tmp_path / "image.qcow2"
    image.write_bytes(b"concrete signed VM image fixture")
    signature = tmp_path / "image.sig"
    subprocess.run(
        [*command, "--detach-sign", "--output", str(signature), str(image)], check=True
    )
    host.KEYRING = keyring
    host.SHA256 = hashlib.sha256(image.read_bytes()).hexdigest()
    host.verify_image(image, signature)
    image.write_bytes(
        b"a different image with matching expected digest but wrong signature"
    )
    host.SHA256 = hashlib.sha256(image.read_bytes()).hexdigest()
    with pytest.raises(subprocess.CalledProcessError):
        host.verify_image(image, signature)
    subprocess.run(
        ["gpgconf", "--homedir", str(keyhome), "--kill", "gpg-agent"], check=True
    )

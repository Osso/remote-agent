import importlib.util
import json
from pathlib import Path
from unittest import TestCase, mock
import tempfile


DIRECTORY = Path(__file__).resolve().parents[1] / "sandbox/openrouter-vm"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load("host_deploy")
vm = load("vm")


class ProvisionTest(TestCase):
    def test_cloud_config_limits_admin_and_uses_repository_proxy(self):
        data = host.cloud_config("ssh-ed25519 client", "private", "ssh-ed25519 host")
        self.assertFalse(data["ssh_pwauth"])
        self.assertEqual(data["ssh_keys"]["ed25519_private"], "private")
        self.assertEqual(
            [(u["name"], u.get("uid")) for u in data["users"]],
            [("root", None), ("agent", 1000)],
        )
        files = {f["path"]: f["content"] for f in data["write_files"]}
        self.assertEqual(
            files["/etc/pacman.d/mirrorlist"],
            "Server = http://10.0.2.100:3129/$repo/os/$arch\n",
        )
        self.assertIn(
            "PermitRootLogin prohibit-password",
            files["/etc/ssh/sshd_config.d/10-openrouter.conf"],
        )

    def test_network_and_disks_are_attached_without_host_shares(self):
        args = vm.command(Path("/home/osso/vm"))
        network = args[args.index("-netdev") + 1]
        self.assertEqual(
            network,
            "user,id=net0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:22230-:22,guestfwd=tcp:10.0.2.100:3128-cmd:python3 /home/osso/vm/relay.py,guestfwd=tcp:10.0.2.100:3129-cmd:python3 /home/osso/vm/repo_proxy.py",
        )
        self.assertNotIn("-virtfs", args)
        self.assertIn("file=/home/osso/vm/disk.qcow2,if=virtio,format=qcow2", args)

    def test_existing_vm_is_updated_without_replacing_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root, stage, unit = base / "vm", base / "stage", base / "unit"
            root.mkdir()
            stage.mkdir()
            (root / "vm.json").write_text(json.dumps(host.config(root)))
            (root / "disk.qcow2").write_bytes(b"existing disk")
            for name in ("seed.iso", "id_ed25519", "known_hosts"):
                (root / name).write_text("existing")
            unit.write_text(host.unit_text(root))
            for name in host.SCRIPTS:
                (stage / name).write_text("new " + name)
            with (
                mock.patch.object(host.shutil, "which", return_value="/usr/bin/tool"),
                mock.patch.object(host, "run") as run,
                mock.patch.object(host, "download") as download,
            ):
                host.install(stage, root, unit)
            self.assertEqual((root / "disk.qcow2").read_bytes(), b"existing disk")
            self.assertEqual((root / "relay.py").read_text(), "new relay.py")
            download.assert_not_called()
            self.assertEqual(
                run.call_args_list[-1].args,
                ("systemctl", "--user", "enable", "--now", "wowdecomp-vm.service"),
            )

    def test_unknown_existing_state_is_refused_without_touching_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "vm"
            stage = base / "stage"
            root.mkdir()
            stage.mkdir()
            (root / "disk.qcow2").write_bytes(b"keep me")
            for name in host.SCRIPTS:
                (stage / name).write_text("script")
            with mock.patch.object(host.shutil, "which", return_value="/usr/bin/tool"):
                with self.assertRaisesRegex(ValueError, "existing VM state"):
                    host.install(stage, root, base / "unit")
            self.assertEqual((root / "disk.qcow2").read_bytes(), b"keep me")

    def test_image_digest_failure_prevents_signature_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image"
            image.write_bytes(b"wrong image")
            with mock.patch.object(host, "run") as run:
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    host.verify_image(image, Path(tmp) / "image.sig")
                run.assert_not_called()

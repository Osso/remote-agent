import importlib.util
import io
import json
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


DIRECTORY = Path(__file__).resolve().parents[1] / "sandbox/openrouter-vm"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load("install_runtime")
host = load("host_deploy")


class RuntimeTest(unittest.TestCase):
    def test_runtime_transfer_and_package_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stage, guest = base / "stage", base / "guest"
            stage.mkdir()
            (guest / "opt").mkdir(parents=True)
            (stage / "tools-packages.txt").write_text("python-capstone\nwine\n")
            (stage / "guest_session.py").write_bytes(b"session bytes")
            arch = base / "arch"
            arch.write_bytes(b"arch bytes")
            config = {
                "known_hosts": "known",
                "private_key": "key",
                "ssh_port": 22,
                "admin_user": "root",
                "host": "localhost",
            }
            packages = []
            removed = []

            def execute(args, **kwargs):
                if args[:2] == ["docker", "create"]:
                    return subprocess.CompletedProcess(args, 0, "a" * 64 + "\n")
                if args[:2] == ["docker", "cp"]:
                    with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                        data = b"exact pi bytes\x00"
                        info = tarfile.TarInfo("pi/pi")
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                elif args[:2] == ["docker", "rm"]:
                    removed.append(True)
                elif args[0] == "ssh":
                    command = shlex.split(args[-1])
                    if command[0] == "tar":
                        target = guest / command[-1].lstrip("/")
                        with tarfile.open(fileobj=kwargs["stdin"], mode="r") as archive:
                            archive.extractall(target, filter="data")
                    elif command[0] == "/usr/local/bin/arch":
                        packages.extend(command[2:])
                        raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0)

            with (
                mock.patch.object(runtime, "ARCH", arch),
                mock.patch.object(runtime.subprocess, "run", side_effect=execute),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    runtime.install_runtime(stage, config, "sha256:" + "1" * 64)
            self.assertEqual((guest / "opt/pi/pi").read_bytes(), b"exact pi bytes\x00")
            self.assertEqual((guest / "usr/local/bin/arch").read_bytes(), b"arch bytes")
            self.assertEqual(
                (guest / "usr/local/lib/openrouter-vm/guest_session.py").stat().st_mode
                & 0o777,
                0o755,
            )
            self.assertEqual(
                packages, ["python-capstone", "wine", *runtime.PI_DEPENDENCIES]
            )
            self.assertTrue(removed)

    def test_rejects_mutable_image_before_running_commands(self):
        with mock.patch.object(runtime.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                runtime.install_runtime(Path("."), {}, "latest")
            run.assert_not_called()

    def test_seed_refresh_preserves_disk_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in {
                "id_ed25519.pub": "client",
                "ssh_host_ed25519_key": "private",
                "ssh_host_ed25519_key.pub": "host",
                "disk.qcow2": "disk",
                "seed.iso": "old",
                "user-data": "old",
            }.items():
                (root / name).write_text(value)

            def make_seed(*args):
                Path(args[args.index("-output") + 1]).write_text("new seed")

            with mock.patch.object(host, "run", side_effect=make_seed):
                host.refresh_seed(root)
            self.assertEqual((root / "disk.qcow2").read_text(), "disk")
            self.assertEqual((root / "ssh_host_ed25519_key").read_text(), "private")
            self.assertEqual((root / "seed.iso").read_text(), "new seed")
            config = json.loads((root / "user-data").read_text().split("\n", 1)[1])
            self.assertEqual(config["ssh_genkeytypes"], ["ed25519"])


if __name__ == "__main__":
    unittest.main()

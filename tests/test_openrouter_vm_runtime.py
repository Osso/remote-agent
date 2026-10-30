import importlib.util
import io
import json
import os
import shlex
import shutil
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
            shutil.copyfile(
                DIRECTORY.parent / "openrouter-runtime/tools-packages.txt",
                stage / "tools-packages.txt",
            )
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
                packages,
                [
                    *(stage / "tools-packages.txt").read_text().split(),
                    *runtime.PI_DEPENDENCIES,
                ],
            )
            self.assertTrue(removed)

    def test_package_retries_use_cached_downloads_and_preserve_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ssh = root / "ssh"
            ssh.write_text("""#!/usr/bin/env python3
import os, pathlib, sys
root = pathlib.Path(os.environ["FAKE_ROOT"])
count = root / "count"
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
(root / "cache").write_text("downloaded")
print("download output", flush=True)
errors = (root / "errors").read_text().split("\\n---\\n")
error = errors[min(n - 1, len(errors) - 1)]
if error:
    print(error, file=sys.stderr)
    sys.exit(1)
(root / "installed").write_text("installed from cache")
""")
            ssh.chmod(0o755)
            config = {
                "known_hosts": "known",
                "private_key": "key",
                "ssh_port": 22,
                "admin_user": "root",
                "host": "localhost",
            }
            timeout = "error: failed retrieving file 'lcms2.pkg.tar.zst' from 10.0.2.100:3129 : Operation too slow. Less than 1 bytes/sec transferred the last 10 seconds\nerror: failed to commit transaction (download library error)"
            cases = [
                (timeout + "\n---\n", 2, True),
                (
                    "error: failed retrieving file 'x' from mirror : HTTP 503\n---\n",
                    2,
                    True,
                ),
                (
                    "error: failed retrieving file 'x' from mirror : HTTP 429\n---\n",
                    2,
                    True,
                ),
                (timeout, 3, False),
                (
                    timeout + "\nerror: invalid or corrupted package (signature)",
                    1,
                    False,
                ),
                (timeout + "\nerror: could not satisfy dependencies", 1, False),
                (timeout + "\nerror: authentication failed", 1, False),
                (timeout + "\nerror: disk full", 1, False),
                ("error: failed retrieving file 'x' from mirror : HTTP 404", 1, False),
            ]
            for error, attempts, succeeds in cases:
                with (
                    self.subTest(error=error),
                    mock.patch.dict(
                        os.environ,
                        {
                            "PATH": str(root) + os.pathsep + os.environ["PATH"],
                            "FAKE_ROOT": str(root),
                        },
                    ),
                    mock.patch.object(runtime.time, "sleep"),
                ):
                    for name in ("count", "cache", "installed"):
                        (root / name).unlink(missing_ok=True)
                    (root / "errors").write_text(error)
                    out, err = io.StringIO(), io.StringIO()
                    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                        if succeeds:
                            runtime.install_packages(config, ["git"])
                        else:
                            with self.assertRaises(
                                subprocess.CalledProcessError
                            ) as raised:
                                runtime.install_packages(config, ["git"])
                            self.assertIn("error:", raised.exception.stderr)
                    self.assertEqual(int((root / "count").read_text()), attempts)
                    self.assertEqual((root / "cache").read_text(), "downloaded")
                    self.assertEqual((root / "installed").exists(), succeeds)
                    self.assertIn("download output", out.getvalue())
                    self.assertIn("attempt 1/3 failed", err.getvalue())
                    self.assertIn("error:", err.getvalue())

    def test_wait_ready_reports_last_real_ssh_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ssh = Path(tmp) / "ssh"
            ssh.write_text(
                "#!/usr/bin/env python3\nimport sys\nprint('cloud-init failed: broken datasource', file=sys.stderr)\nsys.exit(1)\n"
            )
            ssh.chmod(0o755)
            config = {
                "known_hosts": "known",
                "private_key": "key",
                "ssh_port": 22,
                "admin_user": "root",
                "host": "localhost",
            }
            with mock.patch.dict(
                os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"]}
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "cloud-init failed: broken datasource"
                ):
                    runtime.wait_ready(config, timeout=0.2)

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

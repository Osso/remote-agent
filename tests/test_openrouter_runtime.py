import importlib.util
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "sandbox/openrouter-runtime"


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_staging_uses_proxy_and_only_installed_pi_files(tmp_path):
    deploy = load("deploy")
    pi = tmp_path / "pi"
    pi.mkdir()
    for name in (
        "pi",
        "package.json",
        "README.md",
        "CHANGELOG.md",
        "photon_rs_bg.wasm",
        "fs-worker.cjs",
        "theme",
        "assets",
        "export-html",
        "node_modules",
    ):
        (pi / name).write_text("test")
    (pi / "secret").write_text("must not be staged")
    archive = tmp_path / "stage.tar.gz"
    deploy.stage(archive, pi)
    with tarfile.open(archive) as staged:
        settings = json.load(staged.extractfile("settings.json"))
        models = json.load(staged.extractfile("models.json"))
        assert "httpProxy" not in settings
        assert (
            models["providers"]["openrouter"]["baseUrl"]
            == "http://172.30.253.1:3128/api/v1"
        )
        assert {m["id"] for m in models["providers"]["openrouter"]["models"]} == {
            "particle-ai/particle-1",
            "minimax/minimax-m3.1-flash",
        }
        assert "pi/secret" not in staged.getnames()


def test_key_file_permissions_and_contents(tmp_path):
    deploy = load("deploy")
    key = tmp_path / "key"
    key.write_text("secret\n")
    key.chmod(0o600)
    assert deploy.key_bytes(key) == b"secret"
    key.chmod(0o644)
    with pytest.raises(ValueError):
        deploy.key_bytes(key)


def test_cancellation_does_not_touch_clank_container(tmp_path):
    old = load("session")
    conversation = "same-conversation"
    clank = "clank-pi-" + old.container_name(conversation).split("openrouter-pi-")[1]
    name = old.container_name(conversation)
    commands = tmp_path / "commands"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!"
        + sys.executable
        + "\nimport json,sys\nfrom pathlib import Path\np=Path("
        + repr(str(commands))
        + ')\nwith p.open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\nif sys.argv[1] == "inspect": print(json.dumps([{"Config":{"Labels":{"remote-agent.conversation":"same-conversation"}},"State":{"Status":"running"}}]))\n'
    )
    docker.chmod(0o755)
    import os

    result = subprocess.run(
        [sys.executable, str(RUNTIME / "session.py"), "--cancel", conversation],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in commands.read_text().splitlines()]
    assert calls[-1] == ["rm", "--force", "--volumes", name]
    assert all(clank not in call for call in calls)


def test_mapping_preserves_other_projects(tmp_path):
    host = load("host_deploy")
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"clank": {"local": "unchanged"}}))
    result = host.mapping(path, "/home/test/project")
    assert result["clank"] == {"local": "unchanged"}
    assert result["openrouter-sandbox"]["remote"] == "Projects/openrouter-sandbox"
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError):
        host.mapping(path, "/different")

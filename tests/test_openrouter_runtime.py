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
        original = json.loads((ROOT / "sandbox/openrouter/models.json").read_text())
        assert (
            models["providers"]["openrouter"]["models"]
            == original["providers"]["openrouter"]["models"]
        )
        assert "pi/secret" not in staged.getnames()


@pytest.mark.parametrize("update_live", [False, True])
def test_config_only_builds_from_current_image_without_disturbing_runtime(
    tmp_path, monkeypatch, update_live
):
    host = load("host_deploy")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(host, "ROOT", state)
    runtime = {
        "image": "sha256:old",
        "home_volume": "home",
        "secret_file": "/secret",
        "network": "unchanged",
    }
    (state / "runtime.json").write_text(json.dumps(runtime))
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "models.json").write_text('{"new": true}')
    (stage / "settings.json").write_text('{"default": "new"}')
    home = tmp_path / "home"
    home.mkdir()
    (home / "models.json").write_text('{"old": true}')
    (home / "settings.json").write_text('{"default": "minimax"}')

    def docker(*args):
        if args[:3] == ("docker", "image", "inspect"):
            return "sha256:old"
        if args[:3] == ("docker", "image", "tag"):
            return ""
        if args[:2] == ("docker", "build"):
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:new")
            return ""
        assert args[:2] == ("docker", "run")
        script = (
            args[-1]
            .replace("/home/agent/.config/pi/agent", str(home))
            .replace("/opt/sandbox/config/models.json", str(stage / "models.json"))
        )
        subprocess.run([sys.executable, "-c", script], check=True)
        return ""

    monkeypatch.setattr(host, "run", docker)
    host.install_configuration(stage, update_live)
    assert json.loads((state / "runtime.json").read_text()) == {
        **runtime,
        "image": "sha256:new",
    }
    assert (state / "runtime.json").stat().st_mode & 0o777 == 0o600
    assert (home / "settings.json").read_text() == '{"default": "minimax"}'
    assert json.loads((home / "models.json").read_text()) == (
        {"new": True} if update_live else {"old": True}
    )


def test_config_only_staging_does_not_require_pi(tmp_path):
    deploy = load("deploy")
    archive = tmp_path / "stage.tar.gz"
    deploy.stage(archive, tmp_path / "missing-pi", config_only=True)
    with tarfile.open(archive) as staged:
        models = json.load(staged.extractfile("models.json"))
        assert (
            models["providers"]["openrouter"]["baseUrl"]
            == "http://172.30.253.1:3128/api/v1"
        )
        assert not any(name.startswith("pi/") for name in staged.getnames())


def test_launch_replaces_stale_volume_configuration(tmp_path):
    launch = load("launch")
    source = tmp_path / "source"
    destination = tmp_path / "home"
    source.mkdir()
    destination.mkdir()
    for name in ("settings.json", "models.json"):
        (source / name).write_text("new " + name)
        (destination / name).write_text("stale")
    (destination / "history").write_text("preserved")
    launch.install_configuration(source, destination)
    assert (destination / "settings.json").read_text() == "new settings.json"
    assert (destination / "models.json").read_text() == "new models.json"
    assert (destination / "history").read_text() == "preserved"


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

import importlib.util
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "sandbox/openrouter-vm"
CONVERSATION = "e9f22590-027a-452a-bc36-a1312c9b5537"


def load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def guest_fixture(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credential = tmp_path / "credential"
    credential.write_text("test-only-key\n")
    config = home / ".config/pi/agent"
    config.mkdir(parents=True)
    (config / "models.json").write_text("preserved models")
    executable = tmp_path / "pi"
    executable.write_text(
        "#!" + sys.executable + "\n"
        "import json,os,sys,time\n"
        'print(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd(),"home":os.environ["HOME"],"key":os.environ["OPENROUTER_API_KEY"],"color":os.environ.get("COLORTERM")}),flush=True)\n'
        'if "--exit" not in sys.argv: time.sleep(30)\n'
    )
    executable.chmod(0o700)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import importlib.util\nfrom pathlib import Path\n"
        f"s=importlib.util.spec_from_file_location('guest',{str(RUNTIME / 'guest_session.py')!r})\n"
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        f"m.HOME=Path({str(home)!r}); m.WORKSPACE=Path({str(workspace)!r})\n"
        f"m.CREDENTIAL=Path({str(credential)!r}); m.PI={str(executable)!r}\n"
        "m.main()\n"
    )
    return launcher, home, workspace


def test_guest_exec_preserves_cwd_identity_environment_and_configuration(tmp_path):
    launcher, home, workspace = guest_fixture(tmp_path)
    result = subprocess.run(
        [sys.executable, str(launcher), "--session", CONVERSATION, "--exit"],
        env={**os.environ, "COLORTERM": "truecolor"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["argv"][-3:] == ["--session", CONVERSATION, "--exit"]
    assert output["cwd"] == str(workspace)
    assert output["home"] == str(home)
    assert output["key"] == "test-only-key"
    assert output["color"] == "truecolor"
    assert (home / ".config/pi/agent/models.json").read_text() == "preserved models"


def test_conversation_lock_covers_exec_lifetime_and_releases_on_process_exit(tmp_path):
    launcher, _, _ = guest_fixture(tmp_path)
    command = [sys.executable, str(launcher), "--session", CONVERSATION]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([first.stdout], [], [], 5)[0], "fixture never started"
        assert json.loads(first.stdout.readline())["argv"][-1] == CONVERSATION
        second = subprocess.run(command, capture_output=True, text=True, timeout=5)
        assert second.returncode != 0
        assert "already running" in second.stderr
    finally:
        first.terminate()
        first.communicate(timeout=5)
    resumed = subprocess.run(
        [*command, "--exit"], capture_output=True, text=True, timeout=5
    )
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["argv"][-1] == "--exit"


def test_guest_rejects_invalid_or_multiple_conversation_ids():
    guest = load("guest_session")
    for arguments in (
        ["--session", "not-a-uuid"],
        ["--resume"],
        ["--session", CONVERSATION, "--resume", CONVERSATION],
    ):
        with pytest.raises(ValueError):
            guest.conversation_id(arguments)


def test_host_ssh_command_preserves_arguments_and_requests_terminal_only_for_tty():
    import shlex

    host = load("session")
    config = {
        "private_key": "/private/key",
        "known_hosts": "/private/hosts",
        "ssh_port": 22230,
    }
    arguments = ["--session", CONVERSATION, "argument with 'quotes'"]
    for tty in (False, True):
        command = host.ssh_arguments(
            config, arguments, tty, {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
        )
        assert command[1] == ("-tt" if tty else "-T")
        remote = shlex.split(command[-1])
        assert remote[-3:] == arguments
        assert "TERM=xterm-256color" in remote
        assert "COLORTERM=truecolor" in remote
        assert "StrictHostKeyChecking=yes" in command
    with pytest.raises(ValueError):
        host.ssh_arguments(config, [], False, {"TERM": "xterm\ncommand"})

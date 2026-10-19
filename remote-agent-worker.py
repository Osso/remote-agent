#!/usr/bin/env python3
"""Remote JSON worker. New sessions use dtach; legacy records retain tmux."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

PROJECTS_FILE = Path.home() / ".config/remote-agent/remote-projects.json"
CLIPBOARD = ".local/state/remote-agent-clipboard.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
AGENTS = {"pi": "pi", "claude": "i"}


def session_name(name):
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name
    ):
        raise ValueError(
            "name must be 1–64 letters/digits/_/-, starting with a letter/digit"
        )
    return "remote-agent-" + name


def validate_colorterm(value):
    if value not in ("", "truecolor", "24bit"):
        raise ValueError("colorterm must be empty, truecolor or 24bit")
    return "truecolor" if value else ""


def check_private(path, directory):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    permissions = 0o700 if directory else 0o600
    if (
        not expected(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != permissions
    ):
        raise ValueError(f"unexpected filesystem state: {path}")


# Each session runs in the capped agents.slice (agents.slice).
SCOPE = [
    "systemd-run",
    "--user",
    "--scope",
    "--slice=agents.slice",
    "--quiet",
    "--collect",
]
HOSTS = json.loads((PROJECTS_FILE.parent / "remote-hosts.json").read_text())
CARGO_BUILD_JOBS = "6"


def resolve_scope():
    """Respect an existing system slice; never shadow it with a user slice."""
    result = subprocess.run(
        ["systemctl", "show", "agents.slice", "-p", "FragmentPath", "--value"],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("could not resolve agents.slice: " + result.stderr.strip())
    return (
        [arg for arg in SCOPE if arg != "--user"]
        if result.stdout.strip()
        else list(SCOPE)
    )


# Conversation arguments (JSON list), handed from launch() to launch_agent()
# through tmux.
CONVERSATION_ENV = "REMOTE_AGENT_CONVERSATION"
RESUME_FLAGS = {"claude": "--resume", "pi": "--session"}
# Both harnesses take --session-id to create a conversation with a known id.
NEW_FLAG = "--session-id"
STATE_FILES = (
    "prompt.txt",
    "session.json",
    "session.json.tmp",
    "runtime.json",
    "runtime.json.tmp",
    "dtach.sock",
)
STARTUP_TIMEOUT = 5
# Worker --attach exit status when the session is gone and cannot be restarted;
# the controller stops reconnecting on it.
ATTACH_REFUSED = 3


class Worker:
    def __init__(self, home=None, tmux=None, scope=None, host="desktop"):
        self.home = Path.home() if home is None else Path(home)
        self.tmux = ["tmux"] if tmux is None else tmux
        self.scope = scope
        self.jobs = HOSTS[host]["cargo_build_jobs"]
        self.build_host_scripts = HOSTS[host].get("build_host_scripts")
        self.state = self.home / ".local/state/remote-agent"
        self.projects = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))

    def run_tmux(self, *args, check=True):
        result = subprocess.run([*self.tmux, *args], text=True, capture_output=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or "tmux command failed")
        return result

    def dispatch(self, request):
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        if request.get("action") == "projects":
            return {"projects": self.projects}
        if request.get("action") == "clipboard":
            return self.save_clipboard(request.get("png"))
        if request.get("action") == "claude-account":
            return ClaudeAccounts(self.home).dispatch(request)
        if request.get("action") == "list":
            return {"sessions": self.list_sessions()}
        session = session_name(request.get("name"))
        action = request.get("action")
        if action == "start":
            return self.start(session, request)
        record = self.read_record(session)
        if record is not None and record.get("backend", "tmux") == "dtach":
            if action == "attach" and "colorterm" in request:
                record["colorterm"] = validate_colorterm(request["colorterm"])
                atomic_write(
                    self.state / session / "session.json", json.dumps(record).encode()
                )
            return self.dispatch_dtach(session, action)
        if action == "locate":
            exists = (
                self.run_tmux(
                    "has-session", "-t", "=" + session, check=False
                ).returncode
                == 0
            )
            directory = self.state / session
            return {"exists": exists or directory.exists() or directory.is_symlink()}
        if action not in ("status", "logs", "attach", "cancel"):
            raise ValueError("unknown action")
        exists = (
            self.run_tmux("has-session", "-t", "=" + session, check=False).returncode
            == 0
        )
        if action == "cancel":
            return self.cancel(session, exists)
        if action == "attach" and not exists:
            self.restart(session)
            exists = True
        if not exists:
            self.run_tmux("has-session", "-t", "=" + session)
        if action == "status":
            return self.status(session)
        if action == "logs":
            return {
                "session": session,
                "warning": "Logs can include sensitive content.",
                "logs": self.run_tmux(
                    "capture-pane", "-p", "-J", "-S", "-", "-t", "=" + session + ":"
                ).stdout,
            }
        if action == "attach":
            os.execvp(self.tmux[0], [*self.tmux, "attach-session", "-t", "=" + session])

    def cancel(self, session, exists):
        """Stop the session and remove its state. A session whose tmux server
        died (WSL shutdown) leaves only state, which is still cleaned up."""
        directory = self.state / session
        has_state = directory.exists() or directory.is_symlink()
        if not exists and not has_state:
            raise ValueError(f"no such session: {session}")
        if has_state:
            self.check_session_directory(directory)
        if exists:
            self.run_tmux("kill-session", "-t", "=" + session)
        if directory.exists():
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()
        return {"session": session, "cancelled": True}

    def check_session_directory(self, directory):
        check_private(self.state, True)
        check_private(directory, True)
        for file in directory.iterdir():
            if file.name not in STATE_FILES:
                raise ValueError(f"unexpected filesystem state: {file}")
            if file.name == "dtach.sock":
                info = file.lstat()
                if (
                    not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) not in (0o600, 0o700)
                ):
                    raise ValueError(f"unexpected filesystem state: {file}")
            else:
                check_private(file, False)

    def read_record(self, session):
        directory = self.state / session
        if not directory.exists() and not directory.is_symlink():
            return None
        self.check_session_directory(directory)
        path = directory / "session.json"
        return json.loads(path.read_text()) if path.exists() else None

    def dispatch_dtach(self, session, action):
        if action == "locate":
            return {"exists": True}
        if action == "status":
            return self.dtach_status(session)
        if action == "logs":
            raise RuntimeError(
                "Terminal scrollback is unavailable for dtach sessions; dtach does not replay output."
            )
        if action == "cancel":
            self.stop_dtach(session)
            directory = self.state / session
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()
            return {"session": session, "cancelled": True}
        if action == "attach":
            if self.dtach_status(session)["dead"]:
                self.restart(session)
            binary = self.find_dtach()
            # Relative socket names avoid sun_path limits even for long homes/names.
            os.chdir(self.state / session)
            # No Ctrl-L: in Pi that opens the model selector. Same-size WINCH
            # may not redraw Pi; dtach supplies neither screen history nor replay.
            os.execv(binary, [binary, "-a", "dtach.sock", "-r", "winch"])
        raise ValueError("unknown action")

    def dtach_status(self, session):
        directory = self.state / session
        self.check_session_directory(directory)
        path = directory / "runtime.json"
        runtime = json.loads(path.read_text()) if path.exists() else {}
        process = runtime.get("process")
        socket = directory / "dtach.sock"
        dead = not identity_alive(process) or not socket.exists()
        return {
            "session": session,
            "cwd": str(
                self.home / self.projects[self.read_record(session)["repo"]]["remote"]
            ),
            "backend": "dtach",
            "dead": dead,
            "attached": not dead
            and socket.exists()
            and bool(socket.stat().st_mode & 0o100),
            "exit_status": runtime.get("exit_status") if dead else None,
        }

    def find_dtach(self):
        path = f"{self.home}/.local/bin:{os.environ.get('PATH', '')}"
        binary = shutil.which("dtach", path=path)
        if binary is None:
            raise FileNotFoundError("dtach executable not found")
        return binary

    def stop_dtach(self, session):
        directory = self.state / session
        self.check_session_directory(directory)
        path = directory / "runtime.json"
        if not path.exists():
            return
        runtime = json.loads(path.read_text())
        process = runtime["process"]
        current = process_identity(process["pid"])
        if current is not None and current != process and "exit_status" not in runtime:
            raise RuntimeError(
                f"process identity mismatch for {session}; refusing cancellation"
            )
        # Freeze the master first so it cannot spawn a replacement PTY child.
        for identity in (runtime["master"], process):
            terminate_owned_tree(identity)

    def launch_dtach(self, session, repo, agent, conversation, prompt_file):
        binary = self.find_dtach()
        cwd, path, executable = self.agent_launch(repo, agent)
        directory = self.state / session
        self.check_session_directory(directory)
        for name in ("runtime.json", "dtach.sock"):
            (directory / name).unlink(missing_ok=True)
        configured_scope = resolve_scope() if self.scope is None else self.scope
        scope = (
            [*configured_scope, f"--unit={session}-dtach", "--"]
            if configured_scope
            else []
        )
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
            CONVERSATION_ENV: json.dumps(conversation),
            "CARGO_BUILD_JOBS": self.jobs,
        }
        environment["TERM"] = "xterm-256color"
        colorterm = validate_colorterm(self.read_record(session).get("colorterm", ""))
        if colorterm:
            environment["COLORTERM"] = colorterm
        else:
            environment.pop("COLORTERM", None)
        if self.build_host_scripts is not None:
            environment["BUILD_HOST_SCRIPTS"] = self.build_host_scripts
        command = [
            binary,
            "-n",
            "dtach.sock",
            "-r",
            "winch",
            sys.executable,
            str(Path(__file__).resolve()),
            "--dtach-run",
            str(directory),
            str(cwd),
            json.dumps(scope),
            agent,
            executable,
            path,
            str(self.home),
        ]
        if prompt_file is not None:
            command.append(str(prompt_file))
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=errors,
                stderr=errors,
            )
            identity = process_identity(process.pid)
            deadline = time.monotonic() + STARTUP_TIMEOUT
            startup_error = None
            while time.monotonic() < deadline:
                result = process.poll()
                # -n daemonizes: reap the launcher, not the detached master.
                if result == 0 and (directory / "runtime.json").exists():
                    self.check_session_directory(directory)
                    runtime = json.loads((directory / "runtime.json").read_text())
                    startup_error = runtime.get("startup_error")
                    if startup_error:
                        break
                    return
                if result is not None and result != 0:
                    break
                time.sleep(0.02)
            terminate_owned_tree(identity)
            process.wait(timeout=STARTUP_TIMEOUT)
            if (directory / "runtime.json").exists():
                self.stop_dtach(session)
            errors.seek(0)
            message = startup_error or errors.read().decode(errors="replace").strip()
            raise RuntimeError(
                "dtach startup failed: "
                + (message or "timed out waiting for runtime metadata")
            )

    def save_clipboard(self, encoded):
        if not isinstance(encoded, str):
            raise ValueError("png must be base64 text")
        data = base64.b64decode(encoded, validate=True)
        if not data.startswith(PNG_SIGNATURE):
            raise ValueError("clipboard data is not a PNG image")
        path = self.home / CLIPBOARD
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
        return {"clipboard": str(path), "bytes": len(data)}

    def list_sessions(self):
        sessions = self.list_tmux_sessions()
        if not self.state.exists():
            return sessions
        check_private(self.state, True)
        for directory in sorted(self.state.iterdir()):
            record = self.read_record(directory.name)
            if record is None or record.get("backend", "tmux") != "dtach":
                continue
            status = self.dtach_status(directory.name)
            sessions.append(
                {
                    "name": directory.name.removeprefix("remote-agent-"),
                    "project": record["repo"],
                    "harness": record["agent"],
                    "backend": "dtach",
                    "attached": status["attached"],
                    "dead": status["dead"],
                    "exit_status": status["exit_status"],
                }
            )
        return sessions

    def list_tmux_sessions(self):
        result = self.run_tmux(
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{session_attached}\t#{pane_dead}\t#{pane_dead_status}"
            "\t#{pane_start_path}\t#{pane_start_command}",
            check=False,
        )
        if result.returncode:
            # No server, or its socket was never created: no sessions exist.
            if "no server running" in result.stderr or (
                "error connecting to" in result.stderr
                and "No such file or directory" in result.stderr
            ):
                return []
            raise RuntimeError(result.stderr.strip() or "tmux command failed")
        paths = {
            str(self.home / project["remote"]): key
            for key, project in self.projects.items()
        }
        sessions = []
        for line in result.stdout.splitlines():
            session, attached, dead, exit_status, cwd, command = line.split("\t", 5)
            if not session.startswith("remote-agent-"):
                continue
            argv = shlex.split(command)
            sessions.append(
                {
                    "name": session.removeprefix("remote-agent-"),
                    "project": paths.get(cwd),
                    "harness": argv[argv.index("--run") + 1]
                    if "--run" in argv[:-1]
                    else None,
                    "attached": attached != "0",
                    "dead": dead == "1",
                    "exit_status": int(exit_status) if exit_status else None,
                }
            )
        return sessions

    def status(self, session):
        result = self.run_tmux(
            "display-message",
            "-p",
            "-t",
            "=" + session + ":",
            "#{?pane_dead,#{pane_start_path},#{pane_current_path}}\t#{pane_dead}\t#{pane_dead_status}",
        )
        cwd, dead, exit_status = result.stdout.rstrip("\n").split("\t")
        return {
            "session": session,
            "cwd": cwd,
            "dead": dead == "1",
            "exit_status": int(exit_status) if exit_status else None,
        }

    def create_directory(self, session):
        for parent in (self.home / ".local", self.home / ".local/state"):
            if parent.is_symlink():
                raise ValueError(f"unexpected filesystem state: {parent}")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        check_private(self.state, True)
        directory = self.state / session
        directory.mkdir(mode=0o700)
        return directory

    def start(self, session, request):
        repo = request.get("repo")
        agent = request.get("agent")
        if not isinstance(repo, str) or repo not in self.projects:
            raise ValueError("project must be one of: " + ", ".join(self.projects))
        if agent not in ("pi", "claude"):
            raise ValueError("agent harness must be pi|claude")
        resume = request.get("resume")
        if resume is not None and not (
            isinstance(resume, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", resume)
        ):
            raise ValueError("resume must be a conversation id")
        prompt = request.get("prompt")
        if "prompt" in request and (not isinstance(prompt, str) or "\0" in prompt):
            raise ValueError("prompt must be text without NUL bytes")
        self.agent_launch(repo, agent)
        if (
            self.run_tmux("has-session", "-t", "=" + session, check=False).returncode
            == 0
        ):
            raise ValueError(f"session already exists: {session}")
        self.find_dtach()
        colorterm = validate_colorterm(request.get("colorterm", ""))
        directory = self.create_directory(session)
        try:
            record = {
                "repo": repo,
                "agent": agent,
                "backend": "dtach",
                "colorterm": colorterm,
            }
            if resume:
                record["conversation"] = resume
                conversation = [RESUME_FLAGS[agent], resume]
            else:
                record["conversation"] = str(uuid.uuid4())
                conversation = [NEW_FLAG, record["conversation"]]
            write_private(directory / "session.json", json.dumps(record))
            prompt_file = None
            if prompt is not None:
                prompt_file = directory / "prompt.txt"
                write_private(prompt_file, prompt)
            self.launch_dtach(session, repo, agent, conversation, prompt_file)
        except Exception:
            for file in directory.iterdir():
                file.unlink()
            directory.rmdir()
            raise
        return {"session": session, "started": True}

    def restart(self, session):
        """Recreate a session whose tmux server died (WSL/Windows restart),
        resuming the conversation recorded at start; the prompt is not resent."""
        directory = self.state / session
        if not directory.is_dir():
            raise ValueError(f"no such session: {session}")
        self.check_session_directory(directory)
        record_file = directory / "session.json"
        if not record_file.exists():
            raise ValueError(f"no conversation recorded for {session}; start it again")
        record = json.loads(record_file.read_text(encoding="utf-8"))
        agent = record["agent"]
        conversation = [RESUME_FLAGS[agent], record["conversation"]]
        if record.get("backend", "tmux") == "dtach":
            if not self.dtach_status(session)["dead"]:
                raise ValueError(f"session already running: {session}")
            self.stop_dtach(session)
            self.launch_dtach(session, record["repo"], agent, conversation, None)
        else:
            self.launch(session, record["repo"], agent, conversation, None)

    def agent_launch(self, repo, agent):
        """Working directory, PATH and executable for an agent in a project."""
        cwd = self.home / self.projects[repo]["remote"]
        if not cwd.is_dir():
            raise ValueError(f"repo directory does not exist: {cwd}")
        path = f"{self.home}/.local/bin:{self.home}/.cargo/bin:{os.environ.get('PATH', '')}"
        executable = shutil.which(AGENTS[agent], path=path)
        if executable is None:
            raise FileNotFoundError(f"agent executable not found: {AGENTS[agent]}")
        return cwd, path, executable

    def launch(self, session, repo, agent, conversation, prompt_file):
        cwd, path, executable = self.agent_launch(repo, agent)
        launch = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--run",
            agent,
            executable,
            path,
            str(self.home),
        ]
        if prompt_file is not None:
            launch.append(str(prompt_file))
        configured_scope = resolve_scope() if self.scope is None else self.scope
        scope = (
            [*configured_scope, f"--unit={session}", "--"] if configured_scope else []
        )
        runtime = f"/run/user/{os.getuid()}"
        build_environment = (
            ["-e", f"BUILD_HOST_SCRIPTS={self.build_host_scripts}"]
            if self.build_host_scripts is not None
            else []
        )
        self.run_tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(cwd),
            "-e",
            f"XDG_RUNTIME_DIR={runtime}",
            "-e",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
            "-e",
            f"{CONVERSATION_ENV}={json.dumps(conversation)}",
            "-e",
            f"CARGO_BUILD_JOBS={self.jobs}",
            *build_environment,
            *scope,
            *launch,
        )


def process_identity(pid):
    """Linux PID identity survives controller restarts, but never PID/boot reuse."""
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError("invalid process pid")
    path = Path(f"/proc/{pid}")
    try:
        fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if path.stat().st_uid != os.getuid() or fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start": fields[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def identity_alive(identity):
    return identity is not None and process_identity(identity["pid"]) == identity


def terminate_owned_tree(identity):
    """Pin and freeze an owned process tree before killing it, never signal by PID."""
    if not identity_alive(identity):
        return
    pinned = []
    pending = [identity]
    try:
        while pending:
            current = pending.pop()
            try:
                fd = os.pidfd_open(current["pid"])
            except ProcessLookupError:
                continue
            if not identity_alive(current):
                os.close(fd)
                continue
            pinned.append(fd)
            try:
                signal.pidfd_send_signal(fd, signal.SIGSTOP)
            except ProcessLookupError:
                continue
            # Once stopped the parent cannot fork. Descendants are frozen in turn.
            for candidate in Path("/proc").iterdir():
                if not candidate.name.isdigit():
                    continue
                try:
                    fields = (candidate / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[1]) != current["pid"]:
                        continue
                    child = process_identity(int(candidate.name))
                    if child is not None:
                        pending.append(child)
                except (FileNotFoundError, ProcessLookupError):
                    continue
    finally:
        # Children first; pidfds cannot ever target a recycled PID.
        try:
            for fd in reversed(pinned):
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # Exit raced cancellation; no live process remains at this fd.
            # Drain the old master before a restart can bind the same socket.
            deadline = time.monotonic() + STARTUP_TIMEOUT
            waiting = set(pinned)
            while waiting:
                ready, _, _ = select.select(
                    list(waiting), [], [], max(0, deadline - time.monotonic())
                )
                if not ready:
                    raise RuntimeError(
                        "timed out waiting for cancelled dtach processes"
                    )
                waiting.difference_update(ready)
        finally:
            for fd in pinned:
                os.close(fd)


SCOPE_DELEGATE_OVERRIDE = "50-remote-agent-delegate.conf"


def scope_systemctl(manager, *arguments, input=None):
    result = subprocess.run(
        ["systemctl", manager, *arguments],
        input=input,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"scope systemctl {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def exec_dtach_ready(startup_json, command_json):
    directory, runtime = json.loads(startup_json)
    atomic_write(Path(directory) / "runtime.json", json.dumps(runtime).encode())
    command = json.loads(command_json)
    os.execvp(command[0], command)


def exec_dtach_scope(scope_json, command_json, startup_json):
    """Move only this launcher into the stable scope, then exec the harness."""
    scope = json.loads(scope_json)
    manager = "--user" if "--user" in scope else "--system"
    unit = next(
        arg.removeprefix("--unit=") for arg in scope if arg.startswith("--unit=")
    )
    if not unit.endswith(".scope"):
        unit += ".scope"
    properties = dict(
        line.split("=", 1)
        for line in scope_systemctl(
            manager,
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "Delegate",
            "-p",
            "DropInPaths",
        ).splitlines()
    )
    if properties["ActiveState"] == "active":
        if properties["Delegate"] != "yes":
            scope_systemctl(
                manager,
                "edit",
                "--runtime",
                "--stdin",
                f"--drop-in={SCOPE_DELEGATE_OVERRIDE}",
                unit,
                input="[Scope]\nDelegate=yes\n",
            )
            scope_systemctl(manager, "daemon-reload")
        attached = subprocess.run(
            [
                "busctl",
                manager,
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "AttachProcessesToUnit",
                "ssau",
                unit,
                "",
                "1",
                str(os.getpid()),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if attached.returncode:
            raise RuntimeError(
                f"could not attach launcher to {unit}: {attached.stderr.strip()}"
            )
        exec_dtach_ready(startup_json, command_json)
    if properties["ActiveState"] != "inactive":
        raise RuntimeError(
            f"cannot launch {unit}: scope is {properties['ActiveState']}"
        )
    # A collected legacy scope's drop-in otherwise prevents transient recreation.
    # Remove only our stable override, never other resource/admin drop-ins.
    runtime_units = (
        Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
        if manager == "--user"
        else Path("/run/systemd/system")
    )
    override = runtime_units / (unit + ".d") / SCOPE_DELEGATE_OVERRIDE
    if override.exists():
        override.unlink()
        if not any(override.parent.iterdir()):
            override.parent.rmdir()
        scope_systemctl(manager, "daemon-reload")
    os.execvp(
        scope[0],
        [
            *scope[:-1],
            "--property=Delegate=yes",
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--dtach-ready",
            startup_json,
            command_json,
        ],
    )


def run_dtach_agent(
    directory, cwd, scope_json, agent, executable, path, home, prompt_file=None
):
    directory = Path(directory)
    check_private(directory, True)
    runtime = {
        "process": process_identity(os.getpid()),
        "master": process_identity(os.getppid()),
    }
    os.chdir(cwd)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--dtach-agent",
        agent,
        executable,
        path,
        home,
    ]
    if prompt_file is not None:
        command.append(prompt_file)
    scoped = bool(json.loads(scope_json))
    if scoped:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--dtach-scope",
            scope_json,
            json.dumps(command),
            json.dumps([str(directory), runtime]),
        ]
    child = subprocess.Popen(command)
    if not scoped:
        atomic_write(directory / "runtime.json", json.dumps(runtime).encode())
    status = child.wait()
    if (directory / "runtime.json").exists():
        runtime = json.loads((directory / "runtime.json").read_text())
    else:
        runtime["startup_error"] = (
            f"scope launcher exited with status {status} before harness startup"
        )
    runtime["exit_status"] = status if status >= 0 else 128 - status
    atomic_write(directory / "runtime.json", json.dumps(runtime).encode())
    return 0


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def atomic_write(path, data, *, mtime_ns=None):
    if path.exists() and path.read_bytes() == data:
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    if mtime_ns is not None:
        os.utime(temporary, ns=(mtime_ns, mtime_ns))
    temporary.replace(path)


class ClaudeAccounts:
    """Same layout as the laptop's claude-cli: active ~/.claude/.credentials.json,
    owner in .active-profile, saved profiles in <name>.credentials.json."""

    SUFFIX = ".credentials.json"

    def __init__(self, home):
        self.dir = Path(home) / ".claude"
        self.active = self.dir / ".credentials.json"
        self.owner_file = self.dir / ".active-profile"

    def profile_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name
        ):
            raise ValueError("profile must be 1–64 letters/digits/_/-")
        return self.dir / (name + self.SUFFIX)

    def owner(self):
        return self.owner_file.read_text().strip() if self.owner_file.exists() else None

    def dispatch(self, request):
        if "use" in request:
            self.use(request["use"])
        elif "name_current" in request:
            self.name_current(request["name_current"])
        elif request.get("release"):
            self.release()
        return self.listing()

    def listing(self):
        profiles = sorted(
            path.name.removesuffix(self.SUFFIX)
            for path in self.dir.glob("*" + self.SUFFIX)
            if not path.name.startswith(".")
        )
        owner = self.owner()
        if owner and owner not in profiles:
            profiles = sorted([*profiles, owner])
        unnamed = self.active.exists() and owner is None
        return {"active": owner, "unnamed_active": unnamed, "profiles": profiles}

    def save_active(self):
        """Write the active credentials back to their owner; refuse to drop unnamed ones."""
        owner = self.owner()
        if owner is None:
            if self.active.exists():
                raise ValueError(
                    "active credentials have no profile name; "
                    "run `remote-agent claude-account --name-current NAME` first"
                )
            return None
        if self.active.exists():
            target = self.profile_path(owner)
            active_mtime = self.active.stat().st_mtime_ns
            if not target.exists() or active_mtime >= target.stat().st_mtime_ns:
                atomic_write(target, self.active.read_bytes(), mtime_ns=active_mtime)
        return owner

    def use(self, name):
        target = self.profile_path(name)
        self.save_active()
        if not target.exists():
            raise ValueError(
                f"profile not found: {name}; available: {self.listing()['profiles']}"
            )
        target_mtime = target.stat().st_mtime_ns
        atomic_write(self.active, target.read_bytes(), mtime_ns=target_mtime)
        atomic_write(self.owner_file, name.encode())

    def name_current(self, name):
        self.profile_path(name)
        if not self.active.exists():
            raise ValueError("no active credentials to name")
        if self.owner() not in (None, name):
            raise ValueError(f"active credentials already belong to {self.owner()}")
        atomic_write(self.owner_file, name.encode())
        atomic_write(self.profile_path(name), self.active.read_bytes())

    def release(self):
        """Prepare for a fresh login: save the active profile, then clear it."""
        self.save_active()
        self.active.unlink(missing_ok=True)
        self.owner_file.unlink(missing_ok=True)


def exec_login(program, *args):
    """Interactive login over `ssh -t`, in the same environment agents get."""
    home = Path.home()
    os.environ["PATH"] = f"{home}/.local/bin:{os.environ.get('PATH', '')}"
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    os.environ["PI_CODING_AGENT_DIR"] = str(home / ".config/pi/agent")
    executable = str(home / ".local/bin" / program)
    os.execv(executable, [executable, *args])


def launch_agent(agent, executable, path, home, prompt_file, *, tmux=True):
    if tmux:
        # Legacy panes retain immediate exits; dtach records them in its wrapper.
        result = subprocess.run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                os.environ["TMUX_PANE"],
                "remain-on-exit",
                "on",
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
    os.environ["PATH"] = path
    os.environ["PI_CODING_AGENT_DIR"] = str(Path(home) / ".config/pi/agent")
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    os.environ["CARGO_BUILD_JOBS"] = os.environ.get(
        "CARGO_BUILD_JOBS", CARGO_BUILD_JOBS
    )
    argv = [executable, *json.loads(os.environ.pop(CONVERSATION_ENV))]
    if prompt_file is not None:
        check_private(Path(prompt_file).parent, True)
        check_private(Path(prompt_file), False)
        if agent == "pi":
            argv.append("@" + prompt_file)
        else:
            argv.extend(["--", Path(prompt_file).read_text(encoding="utf-8")])
    os.execv(executable, argv)


def wl_paste(args):
    """Stand-in for wl-paste serving the PNG uploaded by `remote-agent paste`,
    covering Claude Code and Pi's type-listing and PNG-reading calls."""
    path = Path.home() / CLIPBOARD
    list_types = args in (["-l"], ["--list-types"])
    if not list_types and args not in (
        ["--type", "image/png"],
        ["--type", "image/png", "--no-newline"],
    ):
        print(f"wl-paste stand-in: unsupported arguments {args}", file=sys.stderr)
        return 1
    if not path.is_file():
        print(
            "No image uploaded; run `remote-agent paste` on the laptop.",
            file=sys.stderr,
        )
        return 1
    if list_types:
        print("image/png")
    else:
        sys.stdout.buffer.write(path.read_bytes())
    return 0


def main():
    if Path(sys.argv[0]).name == "wl-paste":
        return wl_paste(sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--attach", metavar="NAME")
    mode.add_argument("--run", nargs="+", help=argparse.SUPPRESS)
    mode.add_argument("--dtach-run", nargs="+", help=argparse.SUPPRESS)
    mode.add_argument("--dtach-agent", nargs="+", help=argparse.SUPPRESS)
    mode.add_argument("--dtach-scope", nargs=3, help=argparse.SUPPRESS)
    mode.add_argument("--dtach-ready", nargs=2, help=argparse.SUPPRESS)
    mode.add_argument("--claude-login", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--pi-login", metavar="PROVIDER", help=argparse.SUPPRESS)
    parser.add_argument("--host", choices=HOSTS, default="desktop")
    parser.add_argument("--colorterm", choices=("", "truecolor", "24bit"))
    args = parser.parse_args()
    try:
        if args.claude_login:
            exec_login("claude", "auth", "login", "--claudeai")
        if args.pi_login is not None:
            exec_login("pi", "login", args.pi_login)
        if args.run is not None:
            if len(args.run) not in (4, 5) or args.run[0] not in AGENTS:
                raise ValueError("invalid launch arguments")
            launch_agent(*args.run[:4], args.run[4] if len(args.run) == 5 else None)
        if args.dtach_agent is not None:
            launch_agent(
                *args.dtach_agent[:4],
                args.dtach_agent[4] if len(args.dtach_agent) == 5 else None,
                tmux=False,
            )
        if args.dtach_ready is not None:
            exec_dtach_ready(*args.dtach_ready)
        if args.dtach_scope is not None:
            exec_dtach_scope(*args.dtach_scope)
        if args.dtach_run is not None:
            return run_dtach_agent(*args.dtach_run)
        worker = Worker(host=args.host)
        request = (
            {"action": "attach", "name": args.attach}
            if args.attach is not None
            else json.load(sys.stdin)
        )
        if args.attach is not None and args.colorterm is not None:
            request["colorterm"] = args.colorterm
        print(json.dumps(worker.dispatch(request)))
        return 0
    except (OSError, ValueError, RuntimeError, UnicodeError, KeyError) as error:
        if args.dtach_scope is not None:
            directory, runtime = json.loads(args.dtach_scope[2])
            runtime["startup_error"] = str(error)
            atomic_write(Path(directory) / "runtime.json", json.dumps(runtime).encode())
        if args.attach is not None:
            print(f"remote-agent: {error}", file=sys.stderr)
            return ATTACH_REFUSED
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

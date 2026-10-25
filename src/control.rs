use crate::protocol::Launch;
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs::{self, DirBuilder, OpenOptions},
    io::Write,
    os::unix::{
        ffi::OsStrExt,
        fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
        process::CommandExt,
    },
    process::Stdio,
};
use std::{
    path::{Path, PathBuf},
    process::{Child, Command},
};

#[derive(Debug, Serialize, Deserialize)]
pub struct Runtime {
    pub pid: u32,
    pub address: String,
    pub certificate: Vec<u8>,
    pub token: String,
    #[serde(default)]
    pub child_pid: Option<u32>,
    #[serde(default)]
    pub exit_code: Option<i32>,
}
pub fn validate_session_name(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= 128
            && name.as_bytes()[0].is_ascii_alphanumeric()
            && name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-'),
        "session name must contain 1–128 ASCII letters, digits, hyphens or underscores and start with a letter or digit"
    );
    Ok(())
}

pub fn session_dir(home: &Path, name: &str) -> Result<PathBuf> {
    validate_session_name(name)?;
    Ok(home
        .join(".local/state/remote-agent-rs/sessions")
        .join(name))
}

pub fn read_launch(state: &Path) -> Result<Launch> {
    let path = state.join("launch.json");
    serde_json::from_slice(&fs::read(&path).with_context(|| format!("read {}", path.display()))?)
        .with_context(|| format!("parse {}", path.display()))
}

fn private_directory(path: &Path) -> Result<()> {
    DirBuilder::new().recursive(true).mode(0o700).create(path)?;
    ensure!(
        !fs::symlink_metadata(path)?.file_type().is_symlink(),
        "state directory is a symlink: {}",
        path.display()
    );
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

pub fn write_private_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path.parent().context("JSON path has no parent")?;
    private_directory(parent)?;
    let bytes = serde_json::to_vec_pretty(value)?;
    let temporary = parent.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        Ok(())
    })();
    if result.is_err() && temporary.exists() {
        fs::remove_file(&temporary)
            .with_context(|| format!("remove temporary JSON {}", temporary.display()))?;
    }
    result.with_context(|| format!("write private JSON {}", path.display()))
}

pub fn list_sessions(home: &Path) -> Result<Value> {
    let root = home.join(".local/state/remote-agent-rs/sessions");
    let entries = match fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(serde_json::json!([]));
        }
        Err(error) => return Err(error).context("list session directory"),
    };
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry?;
        ensure!(
            entry.file_type()?.is_dir(),
            "unexpected non-directory session entry: {}",
            entry.path().display()
        );
        names.push(
            entry
                .file_name()
                .into_string()
                .map_err(|_| anyhow::anyhow!("session name is not UTF-8"))?,
        );
    }
    names.sort();
    names
        .iter()
        .map(|name| status(home, name))
        .collect::<Result<Vec<_>>>()
        .map(Value::Array)
}

pub fn status(home: &Path, name: &str) -> Result<Value> {
    let state = session_dir(home, name)?;
    let launch = read_launch(&state)?;
    ensure!(
        launch.name == name,
        "launch name does not match session directory"
    );
    let runtime_path = state.join("runtime.json");
    let runtime: Option<Runtime> = match fs::read(&runtime_path) {
        Ok(bytes) => Some(serde_json::from_slice(&bytes).context("parse session runtime")?),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error).context("read session runtime"),
    };
    let server_running = match runtime.as_ref() {
        Some(runtime) => server_matches_state(runtime.pid, &state)?,
        None => false,
    };
    let running = server_running && runtime.as_ref().is_some_and(|r| r.exit_code.is_none());
    Ok(serde_json::json!({
        "name":name,"project":launch.project,"cwd":launch.cwd,"conversation":launch.conversation,
        "server_running":server_running,"running":running,
        "pid":runtime.as_ref().map(|r|r.pid),"address":runtime.as_ref().map(|r|&r.address),
        "child_pid":runtime.as_ref().and_then(|r|r.child_pid),"exit_code":runtime.as_ref().and_then(|r|r.exit_code)
    }))
}

pub fn server_matches_state(pid: u32, state: &Path) -> Result<bool> {
    ensure!(
        cfg!(target_os = "linux"),
        "session server identity requires Linux"
    );
    ensure!(state.is_absolute(), "session state path must be absolute");
    if pid == 0 || pid > i32::MAX as u32 {
        return Ok(false);
    }
    let proc = PathBuf::from(format!("/proc/{pid}"));
    let Some(cmdline) = read_process_file(&proc.join("cmdline"))? else {
        return Ok(false);
    };
    let args: Vec<_> = cmdline
        .strip_suffix(&[0])
        .unwrap_or(&cmdline)
        .split(|b| *b == 0)
        .collect();
    if args.len() != 4
        || args[1] != b"__serve"
        || args[2] != b"--state"
        || args[3] != state.as_os_str().as_bytes()
    {
        return Ok(false);
    }
    let expected = fs::metadata(std::env::current_exe()?)?;
    let actual = match fs::metadata(proc.join("exe")) {
        Ok(actual) => actual,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).context("inspect session server executable"),
    };
    let program = Path::new(std::ffi::OsStr::from_bytes(args[0]));
    if !program.is_absolute() {
        return Ok(false);
    }
    let command_exe = match fs::metadata(program) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).context("inspect recorded server executable"),
    };
    Ok(actual.dev() == expected.dev()
        && actual.ino() == expected.ino()
        && actual.dev() == command_exe.dev()
        && actual.ino() == command_exe.ino())
}

fn read_process_file(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => {
            Err(error).with_context(|| format!("read process identity {}", path.display()))
        }
    }
}

pub fn spawn_session(exe: &Path, launch: &Launch, state: &Path) -> Result<Child> {
    spawn_session_with_runner(
        exe,
        launch,
        state,
        Path::new("systemctl"),
        Path::new("systemd-run"),
    )
}

fn spawn_session_with_runner(
    exe: &Path,
    launch: &Launch,
    state: &Path,
    systemctl: &Path,
    systemd_run: &Path,
) -> Result<Child> {
    ensure!(
        cfg!(target_os = "linux"),
        "session server launch requires Linux"
    );
    ensure!(state.is_absolute(), "session state path must be absolute");
    validate_session_name(&launch.name)?;
    ensure!(
        state.file_name().and_then(|s| s.to_str()) == Some(&launch.name),
        "state directory does not match launch name"
    );
    let user_manager = resolve_user_scope(systemctl)?;
    private_directory(state.parent().context("state has no parent")?)?;
    DirBuilder::new()
        .mode(0o700)
        .create(state)
        .with_context(|| format!("create new session {}", state.display()))?;
    write_private_json(&state.join("launch.json"), launch)?;
    let log = OpenOptions::new()
        .write(true)
        .append(true)
        .create_new(true)
        .mode(0o600)
        .open(state.join("server.log"))?;
    let mut command = Command::new(systemd_run);
    if user_manager {
        command.arg("--user");
    }
    command
        .args(["--scope", "--slice=agents.slice", "--quiet", "--collect"])
        .arg(format!("--unit=remote-agent-rs-{}", launch.name))
        .arg("--")
        .arg(exe)
        .arg("__serve")
        .arg("--state")
        .arg(state)
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    // setsid is async-signal-safe; no allocation or locks are used after fork.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
        .spawn()
        .with_context(|| format!("spawn session server {}", exe.display()))
}

fn resolve_user_scope(systemctl: &Path) -> Result<bool> {
    let output = Command::new(systemctl)
        .args(["show", "agents.slice", "-p", "FragmentPath", "--value"])
        .stdin(Stdio::null())
        .output()
        .context("query agents.slice system fragment")?;
    ensure!(
        output.status.success(),
        "could not resolve agents.slice ({}): {}",
        output.status,
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(std::str::from_utf8(&output.stdout)
        .context("agents.slice FragmentPath is not UTF-8")?
        .trim()
        .is_empty())
}

pub fn build_pi_login_command(pi: &Path, provider: &str) -> Command {
    let mut command = Command::new(pi);
    command.arg("login").arg(provider);
    command
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::Size;
    use std::{fs, os::unix::fs::PermissionsExt};
    fn launch() -> Launch {
        Launch {
            name: "test-1".into(),
            project: "demo".into(),
            cwd: "/tmp".into(),
            pi: "/usr/bin/pi".into(),
            conversation: "abc".into(),
            resume: false,
            prompt: Some("hello".into()),
            size: Size { rows: 24, cols: 80 },
            history_bytes: 1024,
        }
    }
    #[test]
    fn names_cannot_escape_private_namespace() {
        let home = tempfile::tempdir().unwrap();
        for name in ["", ".", "..", "a/b", "a b", "-bad", "a\n"] {
            assert!(validate_session_name(name).is_err(), "{name:?}");
        }
        assert_eq!(
            session_dir(home.path(), "test-1").unwrap(),
            home.path()
                .join(".local/state/remote-agent-rs/sessions/test-1")
        );
    }
    #[test]
    fn private_records_roundtrip_and_report_sessions() {
        let home = tempfile::tempdir().unwrap();
        assert_eq!(list_sessions(home.path()).unwrap(), serde_json::json!([]));
        let state = session_dir(home.path(), "test-1").unwrap();
        write_private_json(&state.join("launch.json"), &launch()).unwrap();
        assert_eq!(
            read_launch(&state).unwrap().prompt.as_deref(),
            Some("hello")
        );
        assert_eq!(
            fs::metadata(&state).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let path = state.join("launch.json");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        write_private_json(&path, &launch()).unwrap();
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let pending = status(home.path(), "test-1").unwrap();
        assert_eq!(pending["running"], false);
        write_private_json(
            &state.join("runtime.json"),
            &Runtime {
                pid: std::process::id(),
                address: "127.0.0.1:1234".into(),
                certificate: vec![1, 2],
                token: "secret".into(),
                child_pid: None,
                exit_code: None,
            },
        )
        .unwrap();
        let runtime_path = state.join("runtime.json");
        let runtime: Runtime = serde_json::from_slice(&fs::read(&runtime_path).unwrap()).unwrap();
        assert_eq!(runtime.pid, std::process::id());
        assert_eq!(runtime.address, "127.0.0.1:1234");
        assert_eq!(runtime.certificate, vec![1, 2]);
        assert_eq!(runtime.token, "secret");
        assert_eq!(runtime.child_pid, None);
        assert_eq!(runtime.exit_code, None);
        assert_eq!(
            fs::metadata(&runtime_path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        #[cfg(target_os = "linux")]
        {
            let running = status(home.path(), "test-1").unwrap();
            // A live PID from an unrelated process must never identify a server.
            assert_eq!(running["running"], false);
            assert_eq!(running["server_running"], false);
            assert_eq!(running["name"], "test-1");
            assert!(running.get("token").is_none());
            assert_eq!(
                list_sessions(home.path())
                    .unwrap()
                    .as_array()
                    .unwrap()
                    .len(),
                1
            );
            write_private_json(&runtime_path, &serde_json::json!({"pid":std::process::id(),"address":"127.0.0.1:1234","certificate":[],"token":"secret","child_pid":123,"exit_code":17})).unwrap();
            let exited = status(home.path(), "test-1").unwrap();
            assert_eq!(exited["exit_code"], 17);
            assert_eq!(exited["child_pid"], 123);
            assert_eq!(exited["running"], false);
        }
        #[cfg(target_os = "macos")]
        {
            assert_eq!(
                server_matches_state(std::process::id(), &state)
                    .unwrap_err()
                    .to_string(),
                "session server identity requires Linux"
            );
            assert_eq!(
                status(home.path(), "test-1").unwrap_err().to_string(),
                "session server identity requires Linux"
            );
            assert_eq!(
                list_sessions(home.path()).unwrap_err().to_string(),
                "session server identity requires Linux"
            );
        }
        assert!(status(home.path(), "absent").is_err());
        fs::write(state.join("runtime.json"), "bad").unwrap();
        assert!(status(home.path(), "test-1").is_err());
    }
    #[cfg(target_os = "linux")]
    fn write_executable(path: &Path, source: &str) {
        fs::write(path, source).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[cfg(target_os = "linux")]
    fn scope_provider(home: &Path, fragment: &str, failure: bool) -> (PathBuf, PathBuf) {
        let systemctl = home.join("systemctl");
        write_executable(
            &systemctl,
            &format!(
                "#!/usr/bin/python3\nimport sys\nassert sys.argv[1:] == ['show','agents.slice','-p','FragmentPath','--value']\nprint({fragment:?})\nprint('manager unavailable' if {failure} else '',file=sys.stderr)\nsys.exit({code})\n",
                failure = if failure { "True" } else { "False" },
                code = if failure { 7 } else { 0 },
            ),
        );
        let runner = home.join("systemd-run");
        write_executable(
            &runner,
            "#!/usr/bin/python3\nimport json,pathlib,subprocess,sys\npathlib.Path(__file__).with_suffix('.json').write_text(json.dumps(sys.argv[1:]))\ni=sys.argv.index('--')\nsubprocess.Popen(sys.argv[i+1:])\n",
        );
        (systemctl, runner)
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_resolution_failure_does_not_launch_a_process() {
        let home = tempfile::tempdir().unwrap();
        let (systemctl, runner) = scope_provider(home.path(), "", true);
        let state = session_dir(home.path(), "test-1").unwrap();
        let error = spawn_session_with_runner(
            Path::new("/does/not/exist"),
            &launch(),
            &state,
            &systemctl,
            &runner,
        )
        .unwrap_err();
        assert!(format!("{error:#}").contains("manager unavailable"));
        assert!(!runner.with_extension("json").exists());
        assert!(!state.exists());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn detached_subprocess_receives_state_and_private_log() {
        let home = tempfile::tempdir().unwrap();
        let exe = home.path().join("fixture");
        write_executable(
            &exe,
            "#!/usr/bin/python3\nimport os,sys,json,time,pathlib\ntime.sleep(0.1)\nprint(json.dumps({'args':sys.argv[1:],'sid':os.getsid(0),'pid':os.getpid(),'stdin':sys.stdin.read()}),flush=True)\nprint('private stderr',file=sys.stderr,flush=True)\npathlib.Path(sys.argv[3],'finished').touch()\n",
        );
        for fragment in ["/etc/systemd/system/agents.slice", ""] {
            let fixture = tempfile::tempdir_in(home.path()).unwrap();
            let (systemctl, runner) = scope_provider(fixture.path(), fragment, false);
            let state = session_dir(fixture.path(), "test-1").unwrap();
            let mut child =
                spawn_session_with_runner(&exe, &launch(), &state, &systemctl, &runner).unwrap();
            assert!(child.wait().unwrap().success());
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while !state.join("finished").exists() {
                assert!(
                    std::time::Instant::now() < deadline,
                    "delegated process did not finish"
                );
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            let args: Vec<String> =
                serde_json::from_slice(&fs::read(runner.with_extension("json")).unwrap()).unwrap();
            let mut expected = Vec::new();
            if fragment.is_empty() {
                expected.push("--user".to_string());
            }
            expected.extend(
                [
                    "--scope",
                    "--slice=agents.slice",
                    "--quiet",
                    "--collect",
                    "--unit=remote-agent-rs-test-1",
                    "--",
                ]
                .map(str::to_owned),
            );
            expected.extend(
                [
                    exe.to_str().unwrap(),
                    "__serve",
                    "--state",
                    state.to_str().unwrap(),
                ]
                .map(str::to_owned),
            );
            assert_eq!(args, expected);
            let log = fs::read_to_string(state.join("server.log")).unwrap();
            let output: Value = serde_json::from_str(log.lines().next().unwrap()).unwrap();
            assert!(log.contains("private stderr"));
            assert_eq!(
                output["args"],
                serde_json::json!(["__serve", "--state", state])
            );
            assert_eq!(output["sid"], child.id());
            assert_ne!(output["pid"], child.id());
            assert_eq!(output["stdin"], "");
            assert_eq!(
                fs::metadata(state.join("server.log"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
            assert_eq!(read_launch(&state).unwrap().conversation, "abc");
            assert!(
                spawn_session_with_runner(&exe, &launch(), &state, &systemctl, &runner).is_err()
            );
        }
    }
    #[test]
    fn provider_login_invokes_pi_without_changing_credentials() {
        let home = tempfile::tempdir().unwrap();
        let pi = home.path().join("pi");
        fs::write(&pi, "#!/bin/sh\nprintf '%s\\n' \"$@\"\n").unwrap();
        fs::set_permissions(&pi, fs::Permissions::from_mode(0o700)).unwrap();
        let output = build_pi_login_command(&pi, "anthropic").output().unwrap();
        assert!(output.status.success());
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            "login\nanthropic\n"
        );
        assert!(output.stderr.is_empty());
    }
}

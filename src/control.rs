use crate::protocol::Launch;
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    fs::{self, DirBuilder, OpenOptions},
    io::Write,
    os::unix::{
        fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
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
    let running = runtime.as_ref().is_some_and(|runtime| {
        if runtime.pid == 0 || runtime.pid > i32::MAX as u32 {
            return false;
        }
        // Signal zero probes existence without changing the server process.
        let result = unsafe { libc::kill(runtime.pid as i32, 0) };
        result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    });
    Ok(
        serde_json::json!({"name":name,"project":launch.project,"cwd":launch.cwd,"conversation":launch.conversation,"running":running,"pid":runtime.as_ref().map(|r|r.pid),"address":runtime.as_ref().map(|r|&r.address)}),
    )
}

pub fn spawn_session(exe: &Path, launch: &Launch, state: &Path) -> Result<Child> {
    validate_session_name(&launch.name)?;
    ensure!(
        state.file_name().and_then(|s| s.to_str()) == Some(&launch.name),
        "state directory does not match launch name"
    );
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
    let mut command = Command::new(exe);
    command
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
            },
        )
        .unwrap();
        let running = status(home.path(), "test-1").unwrap();
        assert_eq!(running["running"], true);
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
        assert!(status(home.path(), "absent").is_err());
        fs::write(state.join("runtime.json"), "bad").unwrap();
        assert!(status(home.path(), "test-1").is_err());
    }
    #[test]
    fn detached_subprocess_receives_state_and_private_log() {
        let home = tempfile::tempdir().unwrap();
        let exe = home.path().join("fixture");
        fs::write(&exe, "#!/usr/bin/env python3\nimport os,sys,json\nprint(json.dumps({'args':sys.argv[1:],'sid':os.getsid(0),'pid':os.getpid(),'stdin':sys.stdin.read()}),flush=True)\n").unwrap();
        fs::set_permissions(&exe, fs::Permissions::from_mode(0o700)).unwrap();
        let state = session_dir(home.path(), "test-1").unwrap();
        let mut child = spawn_session(&exe, &launch(), &state).unwrap();
        assert!(child.wait().unwrap().success());
        let output: Value =
            serde_json::from_slice(&fs::read(state.join("server.log")).unwrap()).unwrap();
        assert_eq!(
            output["args"],
            serde_json::json!(["__serve", "--state", state])
        );
        assert_eq!(output["sid"], output["pid"]);
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
        assert!(spawn_session(&exe, &launch(), &state).is_err());
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

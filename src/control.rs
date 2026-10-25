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
    let scope = ScopeMetadata {
        unit: session_scope_unit(&launch.name)?,
        user_manager,
        invocation_id: None,
    };
    write_private_json(&state.join("scope.json"), &scope)?;
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
        .arg(format!("--unit={}", scope.unit))
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

#[derive(Serialize, Deserialize)]
struct ScopeMetadata {
    unit: String,
    user_manager: bool,
    invocation_id: Option<String>,
}

fn session_scope_unit(name: &str) -> Result<String> {
    validate_session_name(name)?;
    Ok(format!("remote-agent-rs-{}-{name}.scope", unsafe {
        libc::getuid()
    }))
}

fn read_scope_metadata(state: &Path, name: &str) -> Result<ScopeMetadata> {
    let unit = session_scope_unit(name)?;
    ensure!(state.is_absolute(), "session state path must be absolute");
    ensure!(
        state.file_name().and_then(|s| s.to_str()) == Some(name),
        "state directory does not match session name"
    );
    let path = state.join("scope.json");
    let metadata = fs::symlink_metadata(&path)
        .with_context(|| format!("inspect scope metadata {}", path.display()))?;
    ensure!(
        metadata.is_file()
            && metadata.mode() & 0o777 == 0o600
            && metadata.uid() == unsafe { libc::getuid() },
        "scope metadata must be an owned private 0600 file"
    );
    let scope: ScopeMetadata = serde_json::from_slice(&fs::read(&path)?)
        .with_context(|| format!("parse scope metadata {}", path.display()))?;
    ensure!(
        scope.unit == unit,
        "scope metadata unit does not match UID and session name"
    );
    Ok(scope)
}

struct ScopeStatus {
    load_state: String,
    invocation_id: String,
}

fn query_scope(scope: &ScopeMetadata) -> Result<ScopeStatus> {
    let mut command = Command::new("systemctl");
    if scope.user_manager {
        command.arg("--user");
    }
    let output = command
        .args([
            "show",
            &scope.unit,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "InvocationID",
        ])
        .stdin(Stdio::null())
        .output()
        .with_context(|| format!("query scope {}", scope.unit))?;
    let text = std::str::from_utf8(&output.stdout).context("scope properties are not UTF-8")?;
    let mut properties = std::collections::BTreeMap::new();
    for line in text.lines() {
        let (key, value) = line
            .split_once('=')
            .context("scope property is not key=value")?;
        ensure!(
            properties.insert(key, value).is_none(),
            "duplicate scope property {key}"
        );
    }
    let load_state = properties
        .get("LoadState")
        .context("scope query missing LoadState")?;
    ensure!(!load_state.is_empty(), "scope query has empty LoadState");
    let active_state = properties
        .get("ActiveState")
        .context("scope query missing ActiveState")?;
    ensure!(
        !active_state.is_empty(),
        "scope query has empty ActiveState"
    );
    ensure!(
        output.status.success() || *load_state == "not-found",
        "query scope {} failed ({}): {}",
        scope.unit,
        output.status,
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(ScopeStatus {
        load_state: (*load_state).to_owned(),
        invocation_id: properties
            .get("InvocationID")
            .context("scope query missing InvocationID")?
            .to_string(),
    })
}

/// Record ownership only after runtime readiness and successful launcher acknowledgement.
pub fn record_scope_identity(state: &Path, name: &str) -> Result<()> {
    let mut scope = read_scope_metadata(state, name)?;
    let current = query_scope(&scope)?;
    ensure!(
        current.load_state != "not-found",
        "cannot record identity of absent scope {}",
        scope.unit
    );
    ensure!(
        !current.invocation_id.trim().is_empty(),
        "scope {} has empty InvocationID",
        scope.unit
    );
    if let Some(recorded) = &scope.invocation_id {
        ensure!(
            recorded == &current.invocation_id,
            "scope {} InvocationID mismatch",
            scope.unit
        );
    }
    scope.invocation_id = Some(current.invocation_id);
    write_private_json(&state.join("scope.json"), &scope)
}

/// Stop the recorded scope instance, including jobs in separate process groups.
pub fn stop_session_scope(state: &Path, name: &str) -> Result<()> {
    let scope = read_scope_metadata(state, name)?;
    let current = query_scope(&scope)?;
    if current.load_state == "not-found" {
        return Ok(());
    }
    let recorded = scope
        .invocation_id
        .as_deref()
        .context("scope identity is pending; refusing to stop existing unit")?;
    ensure!(
        !recorded.trim().is_empty(),
        "recorded scope InvocationID is empty"
    );
    ensure!(
        recorded == current.invocation_id,
        "scope {} InvocationID mismatch",
        scope.unit
    );
    let mut command = Command::new("systemctl");
    if scope.user_manager {
        command.arg("--user");
    }
    let output = command
        .args(["stop", &scope.unit])
        .stdin(Stdio::null())
        .output()
        .with_context(|| format!("stop scope {}", scope.unit))?;
    ensure!(
        output.status.success(),
        "stop scope {} failed ({}): {}",
        scope.unit,
        output.status,
        String::from_utf8_lossy(&output.stderr).trim()
    );
    Ok(())
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
            environment: std::collections::BTreeMap::new(),
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
                    &format!("--unit=remote-agent-rs-{}-test-1.scope", unsafe {
                        libc::getuid()
                    }),
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
    #[cfg(target_os = "linux")]
    #[test]
    fn scope_api_subprocess() {
        let Some(root) = std::env::var_os("REMOTE_AGENT_SCOPE_FIXTURE") else {
            return;
        };
        let root = PathBuf::from(root);
        let state = session_dir(&root, "test-1").unwrap();
        let result = match std::env::var("REMOTE_AGENT_SCOPE_ACTION").unwrap().as_str() {
            "spawn" => {
                spawn_session(Path::new("/bin/true"), &launch(), &state).and_then(|mut child| {
                    ensure!(child.wait()?.success(), "launcher failed");
                    Ok(())
                })
            }
            "record" => record_scope_identity(&state, "test-1"),
            "stop" => stop_session_scope(&state, "test-1"),
            "bad-name" => stop_session_scope(&state, "../test-1"),
            action => panic!("unknown fixture action {action}"),
        };
        fs::write(
            root.join("result.json"),
            serde_json::to_vec(&result.map_err(|error| format!("{error:#}"))).unwrap(),
        )
        .unwrap();
    }

    #[cfg(target_os = "linux")]
    struct ScopeFixture {
        home: tempfile::TempDir,
        user_manager: bool,
    }

    #[cfg(target_os = "linux")]
    impl ScopeFixture {
        fn new(user_manager: bool) -> Self {
            let home = tempfile::tempdir().unwrap();
            let program = r#"#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / 'events.jsonl').open('a') as log:
    log.write(json.dumps([pathlib.Path(__file__).name, args]) + '\n')
config = json.loads((root / 'config.json').read_text())
if pathlib.Path(__file__).name == 'systemd-run':
    expected = ['--user'] if config['user_manager'] else []
    expected += ['--scope', '--slice=agents.slice', '--quiet', '--collect',
                 '--unit=remote-agent-rs-' + str(os.getuid()) + '-test-1.scope',
                 '--', '/bin/true', '__serve', '--state',
                 str(root / '.local/state/remote-agent-rs/sessions/test-1')]
    assert args == expected, args
    (root / 'unit.json').write_text(json.dumps({'LoadState':'loaded', 'ActiveState':'active',
        'InvocationID':'first-instance', 'jobs':['server', 'separate-process-group-job']}))
    sys.exit(0)
if args == ['show', 'agents.slice', '-p', 'FragmentPath', '--value']:
    print('' if config.get('fragment_user_manager', config['user_manager']) else '/etc/systemd/system/agents.slice')
    sys.exit(0)
prefix = ['--user'] if config['user_manager'] else []
assert args[:len(prefix)] == prefix, args
args = args[len(prefix):]
unit = 'remote-agent-rs-' + str(os.getuid()) + '-test-1.scope'
status = json.loads((root / 'unit.json').read_text())
if args == ['show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'InvocationID']:
    if config.get('query_failure'):
        print('fixture query failure', file=sys.stderr)
        sys.exit(7)
    for key in ['LoadState', 'ActiveState', 'InvocationID']:
        print(key + '=' + status[key])
    sys.exit(1 if status['LoadState'] == 'not-found' else 0)
assert args == ['stop', unit], args
if config.get('stop_failure'):
    print('fixture stop failure', file=sys.stderr)
    sys.exit(7)
assert status['LoadState'] != 'not-found'
status.update(LoadState='not-found', ActiveState='inactive', InvocationID='', jobs=[])
(root / 'unit.json').write_text(json.dumps(status))
"#;
            write_executable(&home.path().join("systemctl"), program);
            write_executable(&home.path().join("systemd-run"), program);
            fs::write(
                home.path().join("config.json"),
                serde_json::to_vec(&serde_json::json!({"user_manager":user_manager})).unwrap(),
            )
            .unwrap();
            Self { home, user_manager }
        }

        fn action(&self, action: &str) -> std::result::Result<(), String> {
            let output = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "control::tests::scope_api_subprocess",
                    "--nocapture",
                ])
                .env("REMOTE_AGENT_SCOPE_FIXTURE", self.home.path())
                .env("REMOTE_AGENT_SCOPE_ACTION", action)
                .env("PATH", self.home.path())
                .output()
                .unwrap();
            assert!(output.status.success(), "{output:?}");
            serde_json::from_slice(&fs::read(self.home.path().join("result.json")).unwrap())
                .unwrap()
        }

        fn state(&self) -> PathBuf {
            session_dir(self.home.path(), "test-1").unwrap()
        }

        fn metadata(&self) -> Value {
            serde_json::from_slice(&fs::read(self.state().join("scope.json")).unwrap()).unwrap()
        }

        fn unit(&self) -> Value {
            serde_json::from_slice(&fs::read(self.home.path().join("unit.json")).unwrap()).unwrap()
        }

        fn set_unit(&self, value: &Value) {
            fs::write(
                self.home.path().join("unit.json"),
                serde_json::to_vec(value).unwrap(),
            )
            .unwrap();
        }

        fn configure(&self, key: &str, value: bool) {
            let path = self.home.path().join("config.json");
            let mut config: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            config[key] = value.into();
            fs::write(path, serde_json::to_vec(&config).unwrap()).unwrap();
        }

        fn stop_count(&self) -> usize {
            fs::read_to_string(self.home.path().join("events.jsonl"))
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str::<Value>(line).unwrap())
                .filter(|event| event[1].as_array().unwrap().iter().any(|arg| arg == "stop"))
                .count()
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_lifecycle_records_chosen_manager_and_stops_owned_jobs() {
        for user_manager in [false, true] {
            let fixture = ScopeFixture::new(user_manager);
            fixture.action("spawn").unwrap();
            assert_eq!(
                fixture.metadata(),
                serde_json::json!({
                    "unit":format!("remote-agent-rs-{}-test-1.scope", unsafe { libc::getuid() }),
                    "user_manager":fixture.user_manager, "invocation_id":null
                })
            );
            assert_eq!(
                fs::metadata(fixture.state().join("scope.json"))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
            // A changed fragment must not change the previously chosen manager.
            fixture.configure("fragment_user_manager", !user_manager);
            fixture.action("record").unwrap();
            assert_eq!(fixture.metadata()["invocation_id"], "first-instance");
            fixture.action("stop").unwrap();
            assert_eq!(fixture.unit()["jobs"], serde_json::json!([]));
            assert_eq!(fixture.stop_count(), 1);
            fixture.action("stop").unwrap();
            assert_eq!(fixture.stop_count(), 1);
            assert_eq!(fixture.metadata()["invocation_id"], "first-instance");
            assert!(fixture.state().join("server.log").exists());
            assert_eq!(read_launch(&fixture.state()).unwrap().conversation, "abc");
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_lifecycle_refuses_replaced_or_pending_instances() {
        for record in [false, true] {
            let fixture = ScopeFixture::new(true);
            fixture.action("spawn").unwrap();
            if record {
                fixture.action("record").unwrap();
                let mut unit = fixture.unit();
                unit["InvocationID"] = "replacement-instance".into();
                fixture.set_unit(&unit);
            }
            let before = fixture.unit();
            let error = fixture.action("stop").unwrap_err();
            assert!(
                error.contains(if record {
                    "InvocationID mismatch"
                } else {
                    "pending"
                }),
                "{error}"
            );
            assert_eq!(fixture.unit(), before);
            assert_eq!(fixture.stop_count(), 0);
            if record {
                assert!(
                    fixture
                        .action("record")
                        .unwrap_err()
                        .contains("InvocationID mismatch")
                );
            }
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_lifecycle_pending_absent_unit_allows_failed_start_cleanup() {
        let fixture = ScopeFixture::new(false);
        fixture.action("spawn").unwrap();
        fixture.set_unit(&serde_json::json!({"LoadState":"not-found", "ActiveState":"inactive", "InvocationID":"", "jobs":[]}));
        fixture.action("stop").unwrap();
        assert_eq!(fixture.stop_count(), 0);
        assert!(fixture.action("record").is_err());
        assert_eq!(fixture.metadata()["invocation_id"], Value::Null);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_lifecycle_refuses_invalid_identity_metadata() {
        for field in ["unit", "invocation_id"] {
            let fixture = ScopeFixture::new(true);
            fixture.action("spawn").unwrap();
            fixture.action("record").unwrap();
            let mut metadata = fixture.metadata();
            metadata[field] = if field == "unit" {
                "remote-agent-rs-999-other.scope"
            } else {
                ""
            }
            .into();
            write_private_json(&fixture.state().join("scope.json"), &metadata).unwrap();
            assert!(fixture.action("stop").is_err());
            assert_eq!(fixture.stop_count(), 0);
            assert!(fixture.action("bad-name").is_err());
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_lifecycle_rejects_empty_runtime_identity() {
        let fixture = ScopeFixture::new(true);
        fixture.action("spawn").unwrap();
        let mut unit = fixture.unit();
        unit["InvocationID"] = "".into();
        fixture.set_unit(&unit);
        assert!(
            fixture
                .action("record")
                .unwrap_err()
                .contains("empty InvocationID")
        );
        assert_eq!(fixture.metadata()["invocation_id"], Value::Null);
        assert!(fixture.action("stop").is_err());
        assert_eq!(fixture.stop_count(), 0);
        assert_eq!(fixture.unit(), unit);
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn scope_lifecycle_manager_failure_preserves_identity_for_retry() {
        let fixture = ScopeFixture::new(false);
        fixture.action("spawn").unwrap();
        fixture.action("record").unwrap();
        let metadata = fixture.metadata();
        let unit = fixture.unit();
        fixture.configure("query_failure", true);
        assert!(fixture.action("stop").is_err());
        assert_eq!(fixture.stop_count(), 0);
        fixture.configure("query_failure", false);
        fixture.configure("stop_failure", true);
        assert!(
            fixture
                .action("stop")
                .unwrap_err()
                .contains("fixture stop failure")
        );
        assert_eq!(fixture.unit(), unit);
        assert_eq!(fixture.metadata(), metadata);
        fixture.configure("stop_failure", false);
        fixture.action("stop").unwrap();
        assert_eq!(fixture.unit()["jobs"], serde_json::json!([]));
        assert_eq!(fixture.metadata(), metadata);
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

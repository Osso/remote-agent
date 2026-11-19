use anyhow::{Context, Result, bail, ensure};
use clap::{Parser, Subcommand};
use remote_agent::{client, config::Config, control, project, protocol::*};
use serde_json::{Value, json};
use std::io::Read;
#[cfg(target_os = "linux")]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Parser)]
#[command(
    version,
    about = "Persistent Pi sessions with complete bounded scrollback"
)]
struct Cli {
    #[arg(long, global = true)]
    host: Option<String>,
    #[arg(long, global = true)]
    user: Option<String>,
    #[command(subcommand)]
    command: Action,
}

#[derive(Subcommand)]
enum Action {
    Start {
        project: String,
        name: Option<String>,
        #[arg(long)]
        resume: Option<String>,
        #[arg(long)]
        prompt_file: Option<PathBuf>,
        #[arg(long)]
        history_bytes: Option<usize>,
    },
    List,
    Status {
        name: String,
    },
    Logs {
        name: String,
    },
    Attach {
        name: String,
    },
    Cancel {
        name: String,
    },
    /// Stop the session service and clear its started mark; `start PROJECT NAME` resumes it.
    Stop {
        name: String,
    },
    /// Restart the session service; its foreground work and service jobs are interrupted.
    Restart {
        name: String,
    },
    Projects,
    /// Create a project checkout locally and on the remote account, then map it.
    Create {
        project: String,
        /// Absolute path of the new local checkout.
        #[arg(long)]
        local: String,
        /// Checkout path relative to the remote account home (default: Projects/<PROJECT>).
        #[arg(long)]
        remote: Option<String>,
    },
    /// Sign in to Claude bridge or another Pi provider on the remote account.
    Login {
        provider: String,
        /// Existing Claude bridge account profile (claude provider only).
        account: Option<String>,
    },
    #[command(name = "__as-user", hide = true)]
    AsUser {
        user: String,
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        arguments: Vec<String>,
    },
    #[command(name = "__rpc", hide = true)]
    Rpc,
    #[command(name = "__serve", hide = true)]
    Serve {
        #[arg(long)]
        state: PathBuf,
    },
    #[command(name = "__login", hide = true)]
    RunLogin {
        provider: String,
        account: Option<String>,
    },
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    if let Err(error) = run(Cli::parse()).await {
        eprintln!("remote-agent: {error:#}");
        std::process::exit(1);
    }
}

async fn run(cli: Cli) -> Result<()> {
    if let Action::AsUser { user, arguments } = &cli.command {
        #[cfg(target_os = "linux")]
        {
            let account = remote_agent::account::read_account(user)?;
            let error = remote_agent::account::build_user_command(&account, arguments)?.exec();
            return Err(error).context("execute isolated target-account command");
        }
        #[cfg(not(target_os = "linux"))]
        bail!(
            "target account {user} operation {:?} requires a Linux server",
            arguments.first()
        );
    }
    if let Action::Serve { state } = &cli.command {
        #[cfg(target_os = "linux")]
        return remote_agent::server::serve(state.clone()).await;
        #[cfg(not(target_os = "linux"))]
        bail!(
            "session server {} requires Linux; this platform supports the client only",
            state.display()
        );
    }
    let home = PathBuf::from(std::env::var_os("HOME").context("HOME is not set")?);
    if matches!(cli.command, Action::Rpc | Action::RunLogin { .. }) {
        std::env::set_current_dir(&home).context("enter target account home")?;
    }
    let config = Config::load(&home)?;
    match cli.command {
        Action::Rpc => {
            ensure!(cfg!(target_os = "linux"), "control servers require Linux");
            let mut data = Vec::new();
            std::io::stdin()
                .take(1024 * 1024 + 1)
                .read_to_end(&mut data)?;
            ensure!(data.len() <= 1024 * 1024, "control request exceeds one MiB");
            let request = serde_json::from_slice(&data).context("parse control request")?;
            let reply = match dispatch(&home, &config, request).await {
                Ok(value) => value,
                Err(error) => json!({"error":format!("{error:#}")}),
            };
            println!("{}", serde_json::to_string(&reply)?);
        }
        Action::RunLogin { provider, account } => {
            let mut command =
                control::build_login_command(&config, &home, &provider, account.as_deref())?;
            let status = if provider == "claude" {
                control::run_claude_login(command)?
            } else {
                command.status()?
            };
            ensure!(status.success(), "{provider} login exited {status}");
        }
        action => {
            let host = match cli.host {
                Some(host) => host,
                None => config.default_host()?,
            };
            run_client_command(&config, &host, cli.user.as_deref(), action).await?;
        }
    }
    Ok(())
}

async fn run_client_command(
    config: &Config,
    host: &str,
    user: Option<&str>,
    action: Action,
) -> Result<()> {
    let request = match action {
        Action::Attach { name } => {
            let code = client::attach(config, host, user, &name).await?;
            ensure!(code == 0, "Pi exited with status {code}");
            return Ok(());
        }
        Action::Login { provider, account } => {
            let arguments: Vec<String> = ["__login".into(), provider]
                .into_iter()
                .chain(account)
                .collect();
            let remote = config.build_ssh_command(host, user, &arguments)?;
            let status = Command::new("ssh")
                .arg("-t")
                .args(remote.get_args())
                .status()?;
            ensure!(status.success(), "remote provider login exited {status}");
            return Ok(());
        }
        Action::Start {
            project,
            name,
            resume,
            prompt_file,
            history_bytes,
        } => {
            let prompt = prompt_file
                .map(std::fs::read_to_string)
                .transpose()
                .context("read initial prompt file")?;
            ControlRequest::Start {
                host: host.to_owned(),
                project,
                name,
                resume,
                prompt,
                history_bytes,
            }
        }
        Action::Projects => ControlRequest::Projects,
        Action::Create {
            project: name,
            local,
            remote,
        } => {
            let remote = remote.unwrap_or_else(|| format!("Projects/{name}"));
            create_project(config, host, user, &name, &local, &remote).await?;
            return Ok(());
        }
        Action::List => ControlRequest::List,
        Action::Status { name } => ControlRequest::Status { name },
        Action::Logs { name } => ControlRequest::Logs { name },
        Action::Cancel { name } => ControlRequest::Cancel { name },
        Action::Stop { name } => ControlRequest::Stop { name },
        Action::Restart { name } => ControlRequest::Restart { name },
        _ => bail!("command is not a client action"),
    };
    let response = client::query_remote(config, host, user, &request).await?;
    if let Some(logs) = response.get("logs").and_then(Value::as_str) {
        print!("{logs}");
    } else {
        println!("{}", serde_json::to_string_pretty(&response)?);
    }
    Ok(())
}

async fn create_project(
    config: &Config,
    host: &str,
    user: Option<&str>,
    name: &str,
    local: &str,
    remote: &str,
) -> Result<()> {
    prepare_local(config, name, local, remote)?;
    let request = ControlRequest::Create {
        project: name.into(),
        local: local.into(),
        remote: remote.into(),
    };
    let reply = client::query_remote(config, host, user, &request)
        .await
        .with_context(|| {
            format!("local checkout and mapping are ready at {local}; remote creation failed")
        })?;
    println!("{}", serde_json::to_string_pretty(&reply)?);
    Ok(())
}

fn prepare_local(config: &Config, name: &str, local: &str, remote: &str) -> Result<()> {
    let map = config.projects_path();
    let mut projects = config.read_projects()?;
    if project::registered(&projects, name, local, remote)? {
        return Ok(());
    }
    project::create_checkout(Path::new(local))?;
    project::register(&map, &mut projects, name, local, remote)?;
    Ok(())
}

async fn dispatch(home: &Path, config: &Config, request: ControlRequest) -> Result<Value> {
    match request {
        ControlRequest::Start {
            host,
            project,
            name,
            resume,
            prompt,
            history_bytes,
        } => {
            let name = name.unwrap_or_else(|| project.clone());
            let state = control::session_dir(home, &name)?;
            if state.exists() {
                let has_launch_options =
                    resume.is_some() || prompt.is_some() || history_bytes.is_some();
                return start_existing_session(home, &state, &name, &project, has_launch_options)
                    .await;
            }
            let (cwd, pi) = config.remote_project(&project)?;
            let history_bytes = history_bytes.unwrap_or(DEFAULT_HISTORY_BYTES);
            ensure!(
                history_bytes > 0 && history_bytes <= DEFAULT_HISTORY_BYTES,
                "history_bytes must be between 1 and {DEFAULT_HISTORY_BYTES}"
            );
            let resuming = resume.is_some();
            let conversation = resume.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
            ensure!(
                !conversation.is_empty() && !conversation.contains('\0'),
                "invalid conversation identity"
            );
            let launch = Launch {
                name,
                project,
                cwd,
                pi,
                conversation,
                resume: resuming,
                environment: config.launch_environment(&host)?,
                prompt,
                size: Size { rows: 24, cols: 80 },
                history_bytes,
                started: true,
            };
            let child = control::start_session_unit(&launch, &state)?;
            wait_for_runtime(&state, child).await?;
            control::status(home, &launch.name)
        }
        ControlRequest::List => Ok(json!({"sessions":control::list_sessions(home)?})),
        ControlRequest::Status { name } => control::status(home, &name),
        ControlRequest::Projects => Ok(json!({"projects":config.list_projects()})),
        ControlRequest::Create {
            project: name,
            local,
            remote,
        } => {
            let map = config.projects_path();
            let mut projects = config.read_projects()?;
            project::create_remote(home, &map, &mut projects, &name, &local, &remote)
        }
        ControlRequest::Logs { name } => read_logs(&read_running_ticket(home, &name)?).await,
        ControlRequest::Attach { name } => {
            Ok(serde_json::to_value(read_running_ticket(home, &name)?)?)
        }
        ControlRequest::Cancel { name } => cancel_session(home, config, &name).await,
        ControlRequest::Stop { name } => stop_session(home, &name).await,
        ControlRequest::Restart { name } => restart_session(home, &name).await,
        ControlRequest::PiLogin { .. } => bail!("provider login needs a TTY; use login PROVIDER"),
    }
}

async fn restart_session(home: &Path, name: &str) -> Result<Value> {
    let state = control::session_dir(home, name)?;
    let mut launch = control::read_launch(&state)?;
    ensure!(
        launch.name == name,
        "launch name does not match session directory"
    );
    ensure!(
        launch.started,
        "session {name} is not started; run `remote-agent start {} {name}`",
        launch.project
    );
    let previous = control::status(home, name)?;
    if let Some(pid) = previous["pid"].as_u64() {
        let pid = u32::try_from(pid).context("invalid recorded server PID")?;
        if process_is_live(pid)? {
            ensure!(
                control::server_matches_state(pid, &state)?,
                "refusing to restart: recorded PID does not own this session"
            );
        }
    }
    launch.resume = true;
    launch.prompt = None;
    control::write_private_json(&state.join("launch.json"), &launch)?;
    control::restart_session_unit(name)?;
    for _ in 0..100 {
        let status = control::status(home, name)?;
        if status["running"] == true && status["pid"] != previous["pid"] {
            return Ok(status);
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    bail!("restarted session {name} did not become ready")
}

const RUNTIME_READY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(60);
const RUNTIME_READY_POLL: std::time::Duration = std::time::Duration::from_millis(50);

async fn wait_for_runtime(state: &Path, child: std::process::Child) -> Result<()> {
    wait_for_runtime_with(state, child, RUNTIME_READY_TIMEOUT, RUNTIME_READY_POLL).await
}

async fn wait_for_runtime_with(
    state: &Path,
    mut child: std::process::Child,
    timeout: std::time::Duration,
    poll: std::time::Duration,
) -> Result<()> {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if state.join("runtime.json").is_file() {
            return Ok(());
        }
        if let Some(status) = child.try_wait()?
            && !status.success()
        {
            let log = std::fs::read_to_string(state.join("server.log")).unwrap_or_default();
            bail!("session launcher exited {status}: {log}");
        }
        if std::time::Instant::now() >= deadline {
            break;
        }
        tokio::time::sleep(poll).await;
    }
    bail!(
        "session startup timed out after {}s; the unit may still be starting, inspect {}",
        timeout.as_secs(),
        state.join("server.log").display()
    )
}

fn read_running_ticket(home: &Path, name: &str) -> Result<Ticket> {
    let status = control::status(home, name)?;
    ensure!(
        status["server_running"] == true,
        "session server is not running; start a new named session or explicitly resume its conversation"
    );
    let path = control::session_dir(home, name)?.join("runtime.json");
    let runtime: control::Runtime = serde_json::from_slice(&std::fs::read(path)?)?;
    Ok(Ticket {
        address: runtime.address,
        certificate: runtime.certificate,
        token: runtime.token,
    })
}

async fn read_logs(ticket: &Ticket) -> Result<Value> {
    let update = client::fetch_snapshot(ticket).await?;
    let mut lines = Vec::new();
    if update.expired {
        lines.push("[earlier output expired]".to_string());
    }
    for row in update.scrolled {
        let mut parser = vt100::Parser::new(1.try_into()?, 1000.try_into()?, 0);
        parser.process(&row);
        lines.push(parser.screen().contents());
    }
    let mut parser = vt100::Parser::new(
        update.size.rows.try_into()?,
        update.size.cols.try_into()?,
        0,
    );
    parser.process(&update.screen);
    lines.push(parser.screen().contents());
    Ok(json!({"logs":format!("{}\n", lines.join("\n"))}))
}

async fn cancel_session(home: &Path, config: &Config, name: &str) -> Result<Value> {
    let state = control::session_dir(home, name)?;
    let launch = control::read_launch(&state)?;
    ensure!(
        launch.name == name,
        "session name does not match launch record"
    );
    let hook = config.cancel_hook(&launch.project)?;
    let conversation = launch.conversation.clone();
    stop_session_server(&state, launch).await?;
    invoke_cancel_hook(hook.as_deref(), &conversation)?;
    Ok(json!({"cancelled":name}))
}

async fn stop_session(home: &Path, name: &str) -> Result<Value> {
    let state = control::session_dir(home, name)?;
    let launch = control::read_launch(&state)?;
    ensure!(
        launch.name == name,
        "session name does not match launch record"
    );
    stop_session_server(&state, launch).await?;
    control::status(home, name)
}

/// Disable and stop the unit, wait for the recorded server to exit, then clear the started mark.
/// The mark stays set when stopping fails, so the stop can be retried.
async fn stop_session_server(state: &Path, mut launch: Launch) -> Result<()> {
    let runtime_path = state.join("runtime.json");
    let runtime: Option<control::Runtime> = if runtime_path.exists() {
        Some(serde_json::from_slice(&std::fs::read(runtime_path)?)?)
    } else {
        None
    };
    if let Some(runtime) = &runtime
        && process_is_live(runtime.pid)?
    {
        ensure!(
            control::server_matches_state(runtime.pid, state)?,
            "refusing to signal a PID that does not own this session"
        );
    }
    control::stop_session_unit(&launch.name)?;
    if let Some(runtime) = runtime {
        wait_for_session_exit(runtime.pid).await?;
    }
    launch.started = false;
    control::write_private_json(&state.join("launch.json"), &launch)
}

/// Start the unit of an existing session that is not marked started and mark it.
/// A running unmarked session (recorded before the mark existed) is only marked.
async fn start_existing_session(
    home: &Path,
    state: &Path,
    name: &str,
    project: &str,
    has_launch_options: bool,
) -> Result<Value> {
    let mut launch = control::read_launch(state)?;
    ensure!(
        launch.name == name,
        "session name does not match launch record"
    );
    ensure!(
        !launch.started,
        "session {name} already exists and is started"
    );
    ensure!(
        launch.project == project,
        "session {name} belongs to project {}",
        launch.project
    );
    ensure!(
        !has_launch_options,
        "starting existing session {name} resumes its saved conversation; --resume, --prompt-file and --history-bytes do not apply"
    );
    if control::status(home, name)?["server_running"] != true {
        // The stale record would satisfy the wait below before the new server writes its own.
        match std::fs::remove_file(state.join("runtime.json")) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                return Err(error).context("remove stale session runtime");
            }
            _ => {}
        }
    }
    launch.started = true;
    launch.resume = true;
    launch.prompt = None;
    control::write_private_json(&state.join("launch.json"), &launch)?;
    let child = control::resume_session_unit(name, state)?;
    wait_for_runtime(state, child).await?;
    control::status(home, name)
}

fn process_is_live(pid: u32) -> Result<bool> {
    ensure!(
        pid > 0 && pid <= i32::MAX as u32,
        "invalid recorded server PID"
    );
    let stat = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).context("read recorded server state"),
    };
    let fields = stat
        .rsplit_once(')')
        .context("invalid server process stat")?
        .1;
    let status = fields
        .split_whitespace()
        .next()
        .context("missing process state")?;
    Ok(status != "Z" && status != "X")
}

async fn wait_for_session_exit(pid: u32) -> Result<()> {
    for _ in 0..100 {
        if !process_is_live(pid)? {
            return Ok(());
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    bail!("session unit stopped but its recorded process remains alive")
}

fn invoke_cancel_hook(hook: Option<&Path>, conversation: &str) -> Result<()> {
    let Some(hook) = hook else {
        return Ok(());
    };
    let output = Command::new(hook)
        .arg(conversation)
        .output()
        .with_context(|| format!("run Pi cancellation hook {}", hook.display()))?;
    ensure!(
        output.status.success(),
        "Pi cancellation hook failed ({}): {}{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Stdio;

    fn session_dir(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("remote-agent-wait-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create session dir");
        dir
    }

    fn live_child(seconds: &str) -> std::process::Child {
        Command::new("sh")
            .arg("-c")
            .arg(format!("sleep {seconds}"))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn child")
    }

    #[test]
    fn create_prepares_the_local_checkout_and_mapping_once() {
        let home = tempfile::tempdir().unwrap();
        let config_dir = home.path().join(".config/remote-agent");
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::write(
            config_dir.join("remote-hosts.json"),
            r#"{"agent-server":{"ssh":"agent-server","home":"/home/dev","wrap":[],"windows_shell":false,"cargo_build_jobs":"4"}}"#,
        )
        .unwrap();
        std::fs::write(
            config_dir.join("remote-projects.json"),
            r#"{"engine":{"local":"/src/engine","remote":"Projects/engine"}}"#,
        )
        .unwrap();
        let config = Config::load(home.path()).unwrap();
        let checkout = home.path().join("demo");
        let local = checkout.to_str().unwrap();

        prepare_local(&config, "demo", local, "Projects/demo").unwrap();
        assert!(checkout.join(".git").is_dir());
        let map: Value =
            serde_json::from_slice(&std::fs::read(config.projects_path()).unwrap()).unwrap();
        assert_eq!(
            map["demo"],
            json!({"local": local, "remote": "Projects/demo"})
        );
        assert_eq!(map["engine"]["remote"], "Projects/engine");

        prepare_local(&config, "demo", local, "Projects/demo").unwrap();
        let error = prepare_local(&config, "demo", local, "Projects/other").unwrap_err();
        assert!(error.to_string().contains("already mapped"), "{error}");

        let occupied = home.path().join("occupied");
        std::fs::create_dir_all(&occupied).unwrap();
        let error = prepare_local(
            &config,
            "occupied",
            occupied.to_str().unwrap(),
            "Projects/occupied",
        )
        .unwrap_err();
        assert!(error.to_string().contains("already exists"), "{error}");
        assert!(!config.read_projects().unwrap().contains_key("occupied"));
    }

    #[tokio::test]
    async fn waits_past_the_former_five_second_window() {
        let state = session_dir("late");
        std::fs::write(state.join("server.log"), "Created symlink").expect("write log");
        let child = live_child("8");
        let ready = state.clone();
        let writer = tokio::task::spawn_blocking(move || {
            std::thread::sleep(std::time::Duration::from_millis(5600));
            std::fs::write(ready.join("runtime.json"), "{}").expect("write runtime");
        });
        wait_for_runtime_with(
            &state,
            child,
            std::time::Duration::from_secs(20),
            std::time::Duration::from_millis(50),
        )
        .await
        .expect("late runtime must be accepted");
        writer.await.expect("writer task");
    }

    #[tokio::test]
    async fn reports_launcher_failure_with_its_log() {
        let state = session_dir("fail");
        std::fs::write(state.join("server.log"), "Failed to enable unit").expect("write log");
        let child = Command::new("sh")
            .arg("-c")
            .arg("sleep 0.1; exit 3")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn child");
        let error = wait_for_runtime_with(
            &state,
            child,
            std::time::Duration::from_secs(5),
            std::time::Duration::from_millis(10),
        )
        .await
        .expect_err("failed launcher must fail");
        let message = error.to_string();
        assert!(message.contains("session launcher exited"), "{message}");
        assert!(message.contains("Failed to enable unit"), "{message}");
    }

    #[tokio::test]
    async fn times_out_when_runtime_never_appears() {
        let state = session_dir("timeout");
        let child = live_child("3");
        let error = wait_for_runtime_with(
            &state,
            child,
            std::time::Duration::from_millis(200),
            std::time::Duration::from_millis(20),
        )
        .await
        .expect_err("missing runtime must time out");
        assert!(error.to_string().contains("timed out"), "{error}");
    }
}

use anyhow::{Context, Result, bail, ensure};
use clap::{Parser, Subcommand};
use remote_agent_rs::{client, config::Config, control, protocol::*};
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
    /// Restart the session service; its foreground work and service jobs are interrupted.
    Restart {
        name: String,
    },
    Projects,
    PiLogin {
        provider: String,
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
    Login {
        provider: String,
    },
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    if let Err(error) = run(Cli::parse()).await {
        eprintln!("remote-agent-rs: {error:#}");
        std::process::exit(1);
    }
}

async fn run(cli: Cli) -> Result<()> {
    if let Action::AsUser { user, arguments } = &cli.command {
        #[cfg(target_os = "linux")]
        {
            let account = remote_agent_rs::account::read_account(user)?;
            let error = remote_agent_rs::account::build_user_command(&account, arguments)?.exec();
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
        return remote_agent_rs::server::serve(state.clone()).await;
        #[cfg(not(target_os = "linux"))]
        bail!(
            "session server {} requires Linux; this platform supports the client only",
            state.display()
        );
    }
    let home = PathBuf::from(std::env::var_os("HOME").context("HOME is not set")?);
    if matches!(cli.command, Action::Rpc | Action::Login { .. }) {
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
        Action::Login { provider } => {
            let status = control::build_pi_login_command(&config.find_pi()?, &provider).status()?;
            ensure!(status.success(), "Pi login exited {status}");
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
        Action::PiLogin { provider } => {
            let remote = config.build_ssh_command(host, user, &["__login".into(), provider])?;
            let status = Command::new("ssh")
                .arg("-t")
                .args(remote.get_args())
                .status()?;
            ensure!(status.success(), "remote Pi login exited {status}");
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
        Action::List => ControlRequest::List,
        Action::Status { name } => ControlRequest::Status { name },
        Action::Logs { name } => ControlRequest::Logs { name },
        Action::Cancel { name } => ControlRequest::Cancel { name },
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
            };
            let child = control::start_session_unit(&launch, &state)?;
            wait_for_runtime(&state, child).await?;
            control::status(home, &launch.name)
        }
        ControlRequest::List => Ok(json!({"sessions":control::list_sessions(home)?})),
        ControlRequest::Status { name } => control::status(home, &name),
        ControlRequest::Projects => Ok(json!({"projects":config.list_projects()})),
        ControlRequest::Logs { name } => read_logs(&read_running_ticket(home, &name)?).await,
        ControlRequest::Attach { name } => {
            Ok(serde_json::to_value(read_running_ticket(home, &name)?)?)
        }
        ControlRequest::Cancel { name } => cancel_session(home, config, &name).await,
        ControlRequest::Restart { name } => restart_session(home, &name).await,
        ControlRequest::PiLogin { .. } => bail!("Pi login needs a TTY; use pi-login PROVIDER"),
    }
}

async fn restart_session(home: &Path, name: &str) -> Result<Value> {
    let state = control::session_dir(home, name)?;
    let mut launch = control::read_launch(&state)?;
    ensure!(
        launch.name == name,
        "launch name does not match session directory"
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

async fn wait_for_runtime(state: &Path, mut child: std::process::Child) -> Result<()> {
    for _ in 0..100 {
        if state.join("runtime.json").is_file() {
            return Ok(());
        }
        if let Some(status) = child.try_wait()?
            && !status.success()
        {
            let log = std::fs::read_to_string(state.join("server.log"))?;
            bail!("session launcher exited {status}: {log}");
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    bail!(
        "session startup timed out; inspect {}",
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
            control::server_matches_state(runtime.pid, &state)?,
            "refusing to signal a PID that does not own this session"
        );
    }
    control::stop_session_unit(name)?;
    if let Some(runtime) = runtime {
        wait_for_session_exit(runtime.pid).await?;
    }
    invoke_cancel_hook(hook.as_deref(), &launch.conversation)?;
    Ok(json!({"cancelled":name}))
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
    bail!(
        "session unit stopped but its recorded process remains alive; teardown hook was not invoked"
    )
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

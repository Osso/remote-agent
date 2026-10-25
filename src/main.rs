use anyhow::{Context, Result, bail, ensure};
use clap::{Parser, Subcommand};
use remote_agent_rs::{client, config::Config, control, protocol::*};
use serde_json::{Value, json};
use std::io::Read;
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
    Projects,
    PiLogin {
        provider: String,
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

#[tokio::main]
async fn main() {
    if let Err(error) = run(Cli::parse()).await {
        eprintln!("remote-agent-rs: {error:#}");
        std::process::exit(1);
    }
}

async fn run(cli: Cli) -> Result<()> {
    if let Action::Serve { state } = &cli.command {
        #[cfg(target_os = "linux")]
        return remote_agent_rs::server::serve(state.clone()).await;
        #[cfg(not(target_os = "linux"))]
        bail!("session servers require Linux; this platform supports the client only");
    }
    let home = PathBuf::from(std::env::var_os("HOME").context("HOME is not set")?);
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
        Action::Projects => println!("{}", json!({"projects":config.list_projects()})),
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
                project,
                name,
                resume,
                prompt,
                history_bytes,
            }
        }
        Action::List => ControlRequest::List,
        Action::Status { name } => ControlRequest::Status { name },
        Action::Logs { name } => ControlRequest::Logs { name },
        Action::Cancel { name } => ControlRequest::Cancel { name },
        _ => bail!("command is not a client action"),
    };
    let response = client::query_remote(config, host, user, &request)?;
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
                prompt,
                size: Size { rows: 24, cols: 80 },
                history_bytes,
            };
            let child = control::spawn_session(&std::env::current_exe()?, &launch, &state)?;
            wait_for_runtime(&state, child).await?;
            control::status(home, &launch.name)
        }
        ControlRequest::List => Ok(json!({"sessions":control::list_sessions(home)?})),
        ControlRequest::Status { name } => control::status(home, &name),
        ControlRequest::Projects => Ok(json!({"projects":config.list_projects()})),
        ControlRequest::Logs { name } => read_logs(home, &name),
        ControlRequest::Attach { name } => {
            let status = control::status(home, &name)?;
            ensure!(
                status["server_running"] == true,
                "session server is not running; start a new named session or explicitly resume its conversation"
            );
            let path = control::session_dir(home, &name)?.join("runtime.json");
            let runtime: control::Runtime = serde_json::from_slice(&std::fs::read(path)?)?;
            Ok(serde_json::to_value(Ticket {
                address: runtime.address,
                certificate: runtime.certificate,
                token: runtime.token,
            })?)
        }
        ControlRequest::Cancel { name } => cancel_session(home, &name).await,
        ControlRequest::PiLogin { .. } => bail!("Pi login needs a TTY; use pi-login PROVIDER"),
    }
}

async fn wait_for_runtime(state: &Path, mut child: std::process::Child) -> Result<()> {
    for _ in 0..100 {
        if state.join("runtime.json").is_file() {
            return Ok(());
        }
        if let Some(status) = child.try_wait()? {
            let log = std::fs::read_to_string(state.join("server.log"))?;
            bail!("session server exited {status}: {log}");
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    bail!(
        "session startup timed out; inspect {}",
        state.join("server.log").display()
    )
}

fn read_logs(home: &Path, name: &str) -> Result<Value> {
    let state = control::session_dir(home, name)?;
    let update: DisplayUpdate =
        serde_json::from_slice(&std::fs::read(state.join("history.json"))?)?;
    let mut logs = Vec::new();
    for row in update.scrolled {
        logs.extend(row);
        logs.extend_from_slice(b"\r\n");
    }
    logs.extend(update.screen);
    Ok(json!({"logs":String::from_utf8(logs)?}))
}

async fn cancel_session(home: &Path, name: &str) -> Result<Value> {
    let state = control::session_dir(home, name)?;
    let runtime: control::Runtime =
        serde_json::from_slice(&std::fs::read(state.join("runtime.json"))?)?;
    ensure!(
        control::server_matches_state(runtime.pid, &state)?,
        "refusing to signal a PID that does not own this session"
    );
    // Identity was reconciled against the exact native executable and state path.
    if unsafe { libc::kill(runtime.pid as i32, libc::SIGTERM) } < 0 {
        bail!("stop session: {}", std::io::Error::last_os_error());
    }
    for _ in 0..100 {
        if !control::server_matches_state(runtime.pid, &state)? {
            return Ok(json!({"cancelled":name}));
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    bail!("session cancellation timed out; server was not force-killed")
}

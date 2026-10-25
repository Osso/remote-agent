use crate::config::Config;
use crate::local::{InputAction, InputDecoder, TerminalGuard, read_clipboard_png};
use crate::protocol::*;
use crate::terminal::DisplayRenderer;
use anyhow::{Context, Result, bail, ensure};
use quinn::{Connection, Endpoint};
use rustls::pki_types::CertificateDer;
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::net::{SocketAddr, ToSocketAddrs};
use std::process::{Command, Stdio};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use tokio::io::AsyncWriteExt;
use tokio::sync::{mpsc, watch};

pub fn query_remote(
    config: &Config,
    host: &str,
    user: Option<&str>,
    request: &ControlRequest,
) -> Result<serde_json::Value> {
    let mut command = config.build_ssh_command(host, user, &["__rpc".into()])?;
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .context("start SSH control request")?;
    child
        .stdin
        .take()
        .context("SSH stdin missing")?
        .write_all(&serde_json::to_vec(request)?)?;
    let result = child.wait_with_output()?;
    ensure!(
        result.status.success(),
        "remote control failed: {}",
        String::from_utf8_lossy(&result.stderr).trim()
    );
    let reply: serde_json::Value =
        serde_json::from_slice(&result.stdout).context("decode SSH control reply")?;
    if let Some(error) = reply.get("error").and_then(|v| v.as_str()) {
        bail!("{error}");
    }
    Ok(reply)
}

async fn query_remote_async(
    config: &Config,
    host: &str,
    user: Option<&str>,
    request: &ControlRequest,
) -> Result<serde_json::Value> {
    let original = config.build_ssh_command(host, user, &["__rpc".into()])?;
    let mut command = Command::new("ssh");
    command
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
        ])
        .args(original.get_args());
    let mut command = tokio::process::Command::from(command);
    let mut child = command
        .kill_on_drop(true)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .take()
        .context("SSH stdin missing")?
        .write_all(&serde_json::to_vec(request)?)
        .await?;
    let output = child.wait_with_output().await?;
    ensure!(
        output.status.success(),
        "remote control failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let reply: serde_json::Value = serde_json::from_slice(&output.stdout)?;
    if let Some(error) = reply.get("error").and_then(|value| value.as_str()) {
        bail!("{error}");
    }
    Ok(reply)
}

pub fn query_ssh_hostname(config: &Config, host: &str) -> Result<String> {
    let target = config.ssh_target(host)?;
    let output = Command::new("ssh").arg("-G").arg(target).output()?;
    ensure!(
        output.status.success(),
        "resolve SSH host: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout)?
        .lines()
        .find_map(|line| line.strip_prefix("hostname ").map(str::to_owned))
        .context("ssh -G omitted hostname")
}

pub async fn connect(ticket: &Ticket, host: &str) -> Result<(Endpoint, Connection)> {
    let port = ticket
        .address
        .parse::<SocketAddr>()
        .context("invalid ticket address")?
        .port();
    let address = (host, port)
        .to_socket_addrs()?
        .next()
        .context("remote host has no address")?;
    let mut roots = rustls::RootCertStore::empty();
    roots.add(CertificateDer::from(ticket.certificate.clone()))?;
    let config = quinn::ClientConfig::with_root_certificates(Arc::new(roots))?;
    let bind = if address.is_ipv6() {
        "[::]:0"
    } else {
        "0.0.0.0:0"
    };
    let mut endpoint = Endpoint::client(bind.parse()?)?;
    endpoint.set_default_client_config(config);
    let connection = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        endpoint.connect(address, "remote-agent")?,
    )
    .await??;
    Ok((endpoint, connection))
}

struct InputReader {
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl InputReader {
    fn spawn(sender: mpsc::Sender<Vec<u8>>, cancellation: watch::Sender<Option<String>>) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let thread = std::thread::spawn(move || {
            let mut input = std::io::stdin();
            let mut detector = InputDecoder::new();
            while !stopped.load(Ordering::Relaxed) {
                let mut fd = libc::pollfd {
                    fd: 0,
                    events: libc::POLLIN,
                    revents: 0,
                };
                // poll borrows stdin and times out so cleanup can stop this reader.
                let ready = unsafe { libc::poll(&mut fd, 1, 100) };
                if ready < 0 {
                    if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    eprintln!("read terminal: {}", std::io::Error::last_os_error());
                    break;
                }
                if ready == 0 {
                    detector.finish();
                    continue;
                }
                let mut data = [0; 8192];
                match input.read(&mut data) {
                    Ok(0) => {
                        cancellation.send_replace(Some(String::new()));
                        break;
                    }
                    Ok(count) => {
                        if detector
                            .feed(&data[..count])
                            .iter()
                            .any(|action| matches!(action, InputAction::Detach))
                        {
                            cancellation.send_replace(Some(String::new()));
                            break;
                        }
                        if sender.blocking_send(data[..count].to_vec()).is_err() {
                            break;
                        }
                    }
                    Err(error) => {
                        cancellation.send_replace(Some(format!("read terminal: {error}")));
                        break;
                    }
                }
            }
        });
        Self {
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for InputReader {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            if thread.join().is_err() {
                eprintln!("terminal reader panicked");
            }
        }
    }
}

struct NetworkPump {
    connection: Connection,
    reader: tokio::task::JoinHandle<()>,
}

impl Drop for NetworkPump {
    fn drop(&mut self) {
        self.reader.abort();
        self.connection.close(0u32.into(), b"client detached");
    }
}

struct Attachment {
    id: String,
    after: Option<u64>,
    next_input: u64,
    pending: VecDeque<ClientFrame>,
    renderer: DisplayRenderer,
    latest: Option<DisplayUpdate>,
    size: Size,
    decoder: InputDecoder,
    deferred_rows: Vec<Vec<u8>>,
}

pub async fn attach(config: &Config, host: &str, user: Option<&str>, name: &str) -> Result<i32> {
    let request = ControlRequest::Attach { name: name.into() };
    let ticket: Ticket =
        serde_json::from_value(query_remote_async(config, host, user, &request).await?)?;
    let hostname = query_ssh_hostname(config, host)?;
    let terminal = TerminalGuard::enter()?;
    let size = terminal.size()?;
    let mut state = Attachment {
        id: uuid::Uuid::new_v4().to_string(),
        after: None,
        next_input: 1,
        pending: VecDeque::new(),
        renderer: DisplayRenderer::new(size),
        latest: None,
        size,
        decoder: InputDecoder::new(),
        deferred_rows: Vec::new(),
    };
    let (sender, mut input) = mpsc::channel(128);
    let (cancel, mut cancelled) = watch::channel(None);
    let reader = InputReader::spawn(sender, cancel);
    let mut current = ticket;
    let mut attempts = 0;
    let result = {
        let network = async {
            loop {
                match state.run(&current, &hostname, &terminal, &mut input).await {
                    Ok(code) => break Ok(code),
                    Err(error) => {
                        attempts += 1;
                        eprintln!(
                            "\r\nremote-agent-rs: connection interrupted: {error:#}; reconnecting\r"
                        );
                        if attempts > 5 {
                            break Err(error.context("five reconnect attempts failed"));
                        }
                        tokio::time::sleep(std::time::Duration::from_millis(
                            250 * (1 << attempts.min(4)),
                        ))
                        .await;
                        match query_remote_async(config, host, user, &request)
                            .await
                            .and_then(|reply| Ok(serde_json::from_value(reply)?))
                        {
                            Ok(ticket) => current = ticket,
                            Err(error) => {
                                eprintln!("remote-agent-rs: reconnect control: {error:#}\r");
                                continue;
                            }
                        }
                    }
                }
            }
        };
        tokio::pin!(network);
        tokio::select! {
            result = &mut network => result,
            changed = cancelled.changed() => {
                changed.context("terminal cancellation channel closed")?;
                match cancelled.borrow().as_ref() {
                    Some(message) if !message.is_empty() => Err(anyhow::anyhow!(message.clone())),
                    _ => Ok(0),
                }
            }
        }
    };
    drop(input);
    drop(reader);
    drop(terminal);
    result
}

impl Attachment {
    async fn run(
        &mut self,
        ticket: &Ticket,
        host: &str,
        terminal: &TerminalGuard,
        input: &mut mpsc::Receiver<Vec<u8>>,
    ) -> Result<i32> {
        let (_endpoint, connection) = connect(ticket, host).await?;
        let (mut writer, reader) = connection.open_bi().await?;
        send_frame(
            &mut writer,
            &ClientFrame::Hello {
                token: ticket.token.clone(),
                client: self.id.clone(),
                size: self.size,
                after: self.after,
            },
        )
        .await?;
        for frame in &self.pending {
            send_frame(&mut writer, frame).await?;
        }
        let (network, mut incoming) = mpsc::channel(16);
        let network = NetworkPump {
            connection,
            reader: tokio::spawn(receive_frames(reader, network)),
        };
        let result = self.pump(&mut writer, &mut incoming, terminal, input).await;
        drop(network);
        result
    }

    async fn pump(
        &mut self,
        writer: &mut quinn::SendStream,
        incoming: &mut mpsc::Receiver<Result<ServerFrame>>,
        terminal: &TerminalGuard,
        input: &mut mpsc::Receiver<Vec<u8>>,
    ) -> Result<i32> {
        let mut tick = tokio::time::interval(std::time::Duration::from_millis(100));
        loop {
            tokio::select! {
                frame = incoming.recv() => if let Some(code) = self.receive(frame.context("network reader ended")??, writer).await? { return Ok(code); },
                data = input.recv() => match data {
                    Some(data) => {
                        let actions = self.decoder.feed(&data);
                        if self.send_actions(actions, writer).await? { return Ok(0); }
                    },
                    None => { send_frame(writer, &ClientFrame::Detach).await?; return Ok(0); }
                },
                _ = tick.tick() => {
                    let actions = self.decoder.finish();
                    if self.send_actions(actions, writer).await? { return Ok(0); }
                    let size = terminal.size()?;
                    if size != self.size {
                        self.size = size;
                        self.renderer.resize_physical(size);
                        send_frame(writer, &ClientFrame::Resize { size }).await?;
                        if let Some(update) = &self.latest {
                            if update.size.rows <= size.rows && update.size.cols <= size.cols {
                                write_terminal(&self.renderer.render(update)?)?;
                            }
                        }
                    }
                },
            }
        }
    }

    async fn receive(
        &mut self,
        frame: ServerFrame,
        writer: &mut quinn::SendStream,
    ) -> Result<Option<i32>> {
        match frame {
            ServerFrame::Display { mut update } => {
                if update.size.rows > self.size.rows || update.size.cols > self.size.cols {
                    self.deferred_rows.extend(update.scrolled);
                    return Ok(None);
                }
                if !self.deferred_rows.is_empty() {
                    let mut rows = std::mem::take(&mut self.deferred_rows);
                    rows.extend(update.scrolled);
                    update.scrolled = rows;
                }
                write_terminal(&self.renderer.render(&update)?)?;
                self.after = Some(update.sequence);
                self.latest = Some(update);
                send_frame(
                    writer,
                    &ClientFrame::Ack {
                        sequence: self.after.unwrap(),
                    },
                )
                .await?;
            }
            ServerFrame::InputAck { sequence } => {
                self.pending
                    .retain(|frame| input_sequence(frame) > sequence);
            }
            ServerFrame::Exit { code } => return Ok(Some(code)),
            ServerFrame::Error { message } => eprintln!("\r\nremote-agent-rs: {message}\r"),
        }
        Ok(None)
    }

    async fn send_actions(
        &mut self,
        actions: Vec<InputAction>,
        writer: &mut quinn::SendStream,
    ) -> Result<bool> {
        for action in actions {
            let frame = match action {
                InputAction::Detach => {
                    send_frame(writer, &ClientFrame::Detach).await?;
                    return Ok(true);
                }
                InputAction::Bytes(data) => ClientFrame::Input {
                    sequence: self.next_input,
                    data,
                },
                InputAction::PasteImage => match read_clipboard_png() {
                    Ok(png) => ClientFrame::Image {
                        sequence: self.next_input,
                        png,
                    },
                    Err(error) => {
                        eprintln!("\r\nremote-agent-rs: clipboard paste: {error:#}\r");
                        continue;
                    }
                },
            };
            self.next_input += 1;
            self.pending.push_back(frame.clone());
            send_frame(writer, &frame).await?;
        }
        Ok(false)
    }
}

async fn receive_frames(mut reader: quinn::RecvStream, sender: mpsc::Sender<Result<ServerFrame>>) {
    loop {
        let frame = read_frame(&mut reader).await;
        let ended = frame.is_err();
        if sender.send(frame).await.is_err() || ended {
            return;
        }
    }
}

fn input_sequence(frame: &ClientFrame) -> u64 {
    match frame {
        ClientFrame::Input { sequence, .. } | ClientFrame::Image { sequence, .. } => *sequence,
        _ => 0,
    }
}

fn write_terminal(bytes: &[u8]) -> Result<()> {
    let mut output = std::io::stdout().lock();
    output.write_all(bytes)?;
    output.flush()?;
    Ok(())
}

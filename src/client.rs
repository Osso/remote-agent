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

pub async fn query_remote(
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
    deferred: Option<DisplayUpdate>,
    consecutive_failures: u32,
}

pub async fn attach(config: &Config, host: &str, user: Option<&str>, name: &str) -> Result<i32> {
    let request = ControlRequest::Attach { name: name.into() };
    let ticket: Ticket = serde_json::from_value(query_remote(config, host, user, &request).await?)?;
    let hostname = query_ssh_hostname(config, host)?;
    let terminal = TerminalGuard::enter()?;
    let size = terminal.size()?;
    let mut state = Attachment::new(size);
    let (sender, mut input) = mpsc::channel(128);
    let (cancel, mut cancelled) = watch::channel(None);
    let reader = InputReader::spawn(sender, cancel);
    let mut current = ticket;
    let result = {
        let network = async {
            loop {
                match state.run(&current, &hostname, &terminal, &mut input).await {
                    Ok(code) => break Ok(code),
                    Err(error) => {
                        state.consecutive_failures += 1;
                        eprintln!(
                            "\r\nremote-agent-rs: connection interrupted: {error:#}; reconnecting\r"
                        );
                        if state.consecutive_failures > 5 {
                            break Err(error.context("five reconnect attempts failed"));
                        }
                        let entropy = uuid::Uuid::new_v4().as_u128();
                        tokio::time::sleep(reconnect_delay(state.consecutive_failures, entropy))
                            .await;
                        match query_remote(config, host, user, &request)
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
    fn new(size: Size) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            after: None,
            next_input: 1,
            pending: VecDeque::new(),
            renderer: DisplayRenderer::new(size),
            latest: None,
            size,
            decoder: InputDecoder::new(),
            deferred: None,
            consecutive_failures: 0,
        }
    }

    fn prepare_display(&mut self, update: DisplayUpdate) -> Option<DisplayUpdate> {
        // An authorized Hello snapshot confirms recovery even at the same sequence.
        self.consecutive_failures = 0;
        if self
            .after
            .is_some_and(|sequence| update.sequence <= sequence)
        {
            return None;
        }
        let update = fold_deferred_display(self.deferred.take(), update);
        if update.size.rows > self.size.rows || update.size.cols > self.size.cols {
            self.deferred = Some(update);
            return None;
        }
        Some(update)
    }

    fn begin_replay(&mut self, token: String) -> ClientFrame {
        // The server replays everything after the last applied sequence.
        self.deferred = None;
        ClientFrame::Hello {
            token,
            client: self.id.clone(),
            size: self.size,
            after: self.after,
        }
    }

    async fn run(
        &mut self,
        ticket: &Ticket,
        host: &str,
        terminal: &TerminalGuard,
        input: &mut mpsc::Receiver<Vec<u8>>,
    ) -> Result<i32> {
        let hello = self.begin_replay(ticket.token.clone());
        let (_endpoint, connection) = connect(ticket, host).await?;
        let (mut writer, reader) = connection.open_bi().await?;
        send_frame(&mut writer, &hello).await?;
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
                frame = incoming.recv() => if let Some(code) = self.receive(frame.context("network reader ended")??, writer, terminal).await? { return Ok(code); },
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
                    self.refresh_physical_size(terminal, writer).await?;
                },
            }
        }
    }

    async fn refresh_physical_size(
        &mut self,
        terminal: &TerminalGuard,
        writer: &mut quinn::SendStream,
    ) -> Result<()> {
        let size = terminal.size()?;
        if size == self.size {
            return Ok(());
        }
        self.size = size;
        self.renderer.resize_physical(size);
        send_frame(writer, &ClientFrame::Resize { size }).await?;
        if let Some(update) = &self.latest {
            if update.size.rows <= size.rows && update.size.cols <= size.cols {
                write_terminal(&self.renderer.render(update)?)?;
            }
        }
        Ok(())
    }

    async fn receive(
        &mut self,
        frame: ServerFrame,
        writer: &mut quinn::SendStream,
        terminal: &TerminalGuard,
    ) -> Result<Option<i32>> {
        match frame {
            ServerFrame::Display { update } => {
                self.refresh_physical_size(terminal, writer).await?;
                let Some(update) = self.prepare_display(update) else {
                    return Ok(None);
                };
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

fn reconnect_delay(failures: u32, entropy: u128) -> std::time::Duration {
    let ceiling_ms = 250_u64 * (1 << failures.min(4));
    let jitter_window_ms = ceiling_ms / 10;
    let jitter_ms = (entropy % u128::from(jitter_window_ms + 1)) as u64;
    std::time::Duration::from_millis(ceiling_ms - jitter_window_ms + jitter_ms)
}

fn input_sequence(frame: &ClientFrame) -> u64 {
    match frame {
        ClientFrame::Input { sequence, .. } | ClientFrame::Image { sequence, .. } => *sequence,
        _ => 0,
    }
}

fn fold_deferred_display(
    pending: Option<DisplayUpdate>,
    mut update: DisplayUpdate,
) -> DisplayUpdate {
    let Some(mut pending) = pending else {
        return update;
    };
    if !update.reset {
        pending.scrolled.extend(update.scrolled);
        update.scrolled = pending.scrolled;
        update.reset = pending.reset;
        update.expired |= pending.expired;
    }
    pending.control.extend(update.control);
    update.control = pending.control;
    update
}

#[cfg(test)]
mod resize_tests {
    use super::*;

    fn update(sequence: u64, cols: u16, row: &str) -> DisplayUpdate {
        DisplayUpdate {
            sequence,
            size: Size { rows: 4, cols },
            scrolled: vec![row.as_bytes().to_vec()],
            screen: b"\x1b[Hlive viewport".to_vec(),
            control: Vec::new(),
            reset: false,
            expired: false,
        }
    }

    fn native_text(bytes: &[u8]) -> String {
        let mut parser = vt100::Parser::new(6.try_into().unwrap(), 40.try_into().unwrap(), 100);
        parser.process(bytes);
        let count = parser.screen().scrollback_len();
        let mut lines = Vec::new();
        for offset in (1..=count).rev() {
            parser.screen_mut().set_scrollback(offset);
            lines.push(parser.screen().rows(0, 40).next().unwrap());
        }
        parser.screen_mut().set_scrollback(0);
        lines.push(parser.screen().contents());
        lines.join("\n")
    }

    #[test]
    fn duplicate_authorized_snapshot_restores_failure_budget_without_repainting() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        state.after = Some(7);
        state.consecutive_failures = 5;
        state.begin_replay("renewed ticket".into());
        assert_eq!(state.consecutive_failures, 5);
        assert!(
            state
                .prepare_display(update(7, 40, "same snapshot"))
                .is_none()
        );
        assert_eq!(state.consecutive_failures, 0);
        assert_eq!(state.after, Some(7));
    }

    #[test]
    fn deferred_authorized_snapshot_restores_failure_budget_before_resize() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        state.consecutive_failures = 5;
        assert!(
            state
                .prepare_display(update(1, 80, "wide snapshot"))
                .is_none()
        );
        assert_eq!(state.consecutive_failures, 0);
        assert_eq!(state.after, None);
        assert!(state.deferred.is_some());
    }

    #[test]
    fn reconnect_backoff_grows_exponentially_with_bounded_jitter() {
        for (failures, ceiling_ms) in [(1, 500), (2, 1000), (3, 2000), (4, 4000), (5, 4000)] {
            let floor_ms = ceiling_ms * 9 / 10;
            let window = ceiling_ms - floor_ms;
            assert_eq!(reconnect_delay(failures, 0).as_millis(), floor_ms);
            assert_eq!(reconnect_delay(failures, window).as_millis(), ceiling_ms);
            assert_eq!(reconnect_delay(failures, window + 1).as_millis(), floor_ms);
            let delay_ms = reconnect_delay(failures, u128::MAX).as_millis();
            assert!((floor_ms..=ceiling_ms).contains(&delay_ms));
        }
    }

    #[test]
    fn deferred_reset_discards_rows_before_the_reset() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        assert!(
            state
                .prepare_display(update(1, 80, "obsolete row"))
                .is_none()
        );
        let mut reset = update(2, 80, "replacement row");
        reset.reset = true;
        assert!(state.prepare_display(reset).is_none());
        let ready = state.prepare_display(update(3, 40, "later row")).unwrap();
        let bytes = state.renderer.render(&ready).unwrap();
        let text = native_text(&bytes);
        assert!(!text.contains("obsolete row"), "{text}");
        assert!(text.contains("replacement row"), "{text}");
        assert!(text.contains("later row"), "{text}");
        assert!(bytes.windows(4).any(|part| part == b"\x1b[3J"));
    }

    #[test]
    fn deferred_reset_expiration_and_control_reach_native_terminal() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        let mut reset = update(1, 80, "retained row");
        reset.reset = true;
        reset.expired = true;
        reset.control = b"\x1b]2;deferred title\x07".to_vec();
        assert!(state.prepare_display(reset).is_none());
        let mut next = update(2, 80, "next row");
        next.control = b"\x07".to_vec();
        assert!(state.prepare_display(next).is_none());
        let ready = state
            .prepare_display(update(3, 40, "compatible row"))
            .unwrap();
        let bytes = state.renderer.render(&ready).unwrap();
        let text = native_text(&bytes);
        assert!(text.contains("[earlier output expired]"), "{text}");
        assert!(text.contains("retained row"), "{text}");
        assert!(bytes.ends_with(b"\x1b]2;deferred title\x07\x07"));
    }

    #[test]
    fn a_later_reset_replaces_expired_snapshot_metadata() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        let mut expired = update(1, 80, "expired snapshot");
        expired.reset = true;
        expired.expired = true;
        expired.control = b"\x07".to_vec();
        assert!(state.prepare_display(expired).is_none());
        let mut reset = update(2, 80, "new snapshot");
        reset.reset = true;
        assert!(state.prepare_display(reset).is_none());
        let ready = state
            .prepare_display(update(3, 40, "compatible row"))
            .unwrap();
        let bytes = state.renderer.render(&ready).unwrap();
        let text = native_text(&bytes);
        assert!(!text.contains("expired snapshot"), "{text}");
        assert!(!text.contains("earlier output expired"), "{text}");
        assert!(bytes.ends_with(b"\x07"));
    }

    #[test]
    fn reconnect_replays_only_unapplied_rows_once() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        let first = state.prepare_display(update(1, 40, "applied row")).unwrap();
        let mut bytes = state.renderer.render(&first).unwrap();
        state.after = Some(first.sequence);
        assert!(
            state
                .prepare_display(update(2, 80, "unapplied row"))
                .is_none()
        );
        let hello = state.begin_replay("new ticket".into());
        assert!(matches!(hello, ClientFrame::Hello { after: Some(1), .. }));
        assert!(
            state
                .prepare_display(update(2, 80, "unapplied row"))
                .is_none()
        );
        let ready = state
            .prepare_display(update(3, 40, "compatible row"))
            .unwrap();
        bytes.extend(state.renderer.render(&ready).unwrap());
        let text = native_text(&bytes);
        assert_eq!(text.matches("applied row").count(), 2, "{text}");
        assert_eq!(text.matches("unapplied row").count(), 1, "{text}");
    }

    #[test]
    fn applied_duplicate_is_ignored_before_it_can_be_deferred() {
        let mut state = Attachment::new(Size { rows: 6, cols: 40 });
        let first = state.prepare_display(update(1, 40, "once only")).unwrap();
        let mut bytes = state.renderer.render(&first).unwrap();
        state.after = Some(first.sequence);
        assert!(state.prepare_display(update(1, 80, "once only")).is_none());
        let next = state.prepare_display(update(2, 40, "new row")).unwrap();
        bytes.extend(state.renderer.render(&next).unwrap());
        let text = native_text(&bytes);
        assert_eq!(text.matches("once only").count(), 1, "{text}");
    }
}

fn write_terminal(bytes: &[u8]) -> Result<()> {
    let mut output = std::io::stdout().lock();
    output.write_all(bytes)?;
    output.flush()?;
    Ok(())
}

use crate::control::{Runtime, read_launch, write_private_json};
use crate::protocol::*;
use crate::pty::{Pty, PtyEvent};
use crate::terminal::TerminalState;
use anyhow::{Context, Result, bail, ensure};
use quinn::{Connection, Endpoint};
use rustls::pki_types::PrivatePkcs8KeyDer;
use serde_json::json;
use std::collections::HashMap;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use subtle::ConstantTimeEq;
use tokio::sync::mpsc;

struct Peer {
    connection: String,
    ready: bool,
    size: Size,
    sender: mpsc::Sender<ServerFrame>,
}

enum Event {
    Join {
        id: String,
        connection: String,
        size: Size,
        after: Option<u64>,
        sender: mpsc::Sender<ServerFrame>,
    },
    Frame {
        id: String,
        connection: String,
        frame: ClientFrame,
    },
    Leave {
        id: String,
        connection: String,
    },
    Logs {
        reply: tokio::sync::oneshot::Sender<DisplayUpdate>,
    },
}

struct Acceptor(tokio::task::JoinHandle<()>);

impl Drop for Acceptor {
    fn drop(&mut self) {
        self.0.abort();
    }
}

struct Session {
    state: PathBuf,
    terminal: TerminalState,
    pty: Pty,
    peers: HashMap<String, Peer>,
    input_positions: HashMap<String, u64>,
    size: Size,
    exit_code: Option<i32>,
}

pub async fn serve(state: PathBuf) -> Result<()> {
    ensure!(cfg!(target_os = "linux"), "session servers require Linux");
    let mut launch = read_launch(&state)?;
    if let Some(prompt) = launch.prompt.take() {
        let path = state.join("prompt.txt");
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        file.write_all(prompt.as_bytes())?;
        // The initial prompt belongs to the first launch only. A unit restart
        // (e.g. at boot) must reopen the conversation without submitting it again.
        write_private_json(&state.join("launch.json"), &launch)?;
        launch.prompt = Some(format!("@{}", path.display()));
    }
    launch.size.validate()?;
    ensure!(
        launch.history_bytes > 0 && launch.history_bytes <= DEFAULT_HISTORY_BYTES,
        "history_bytes must be between 1 and {DEFAULT_HISTORY_BYTES}"
    );
    let (endpoint, certificate) = bind_endpoint()?;
    let token = format!("{}{}", uuid::Uuid::new_v4(), uuid::Uuid::new_v4());
    let runtime = Runtime {
        pid: std::process::id(),
        address: endpoint.local_addr()?.to_string(),
        certificate,
        token: token.clone(),
        child_pid: None,
        exit_code: None,
    };
    let terminal = TerminalState::new(launch.size, launch.history_bytes)?;
    let (output_sender, output) = mpsc::channel(64);
    let pty = Pty::spawn(&launch, output_sender)?;
    write_private_json(
        &state.join("runtime.json"),
        &json!({"pid":runtime.pid,"address":runtime.address,"certificate":runtime.certificate,"token":runtime.token,"child_pid":pty.child.id(),"exit_code":null}),
    )?;
    let mut session = Session {
        state,
        terminal,
        pty,
        peers: HashMap::new(),
        input_positions: HashMap::new(),
        size: launch.size,
        exit_code: None,
    };
    let (events_sender, events) = mpsc::channel(128);
    let accepted = endpoint.clone();
    let acceptor = Acceptor(tokio::spawn(async move {
        accept_connections(accepted, token, events_sender).await;
    }));
    let result = session.run(output, events).await;
    endpoint.close(0u32.into(), b"session stopped");
    drop(acceptor);
    let terminated = session.pty.terminate();
    result.and(terminated)
}

fn bind_endpoint() -> Result<(Endpoint, Vec<u8>)> {
    let certificate = rcgen::generate_simple_self_signed(vec!["remote-agent".into()])?;
    let der = certificate.cert.der().clone();
    let key = PrivatePkcs8KeyDer::from(certificate.key_pair.serialize_der());
    let mut config = quinn::ServerConfig::with_single_cert(vec![der.clone()], key.into())?;
    let mut transport = quinn::TransportConfig::default();
    transport.keep_alive_interval(Some(std::time::Duration::from_secs(5)));
    config.transport_config(std::sync::Arc::new(transport));
    Ok((
        Endpoint::server(config, "0.0.0.0:0".parse()?)?,
        der.to_vec(),
    ))
}

async fn accept_connections(endpoint: Endpoint, token: String, events: mpsc::Sender<Event>) {
    while let Some(incoming) = endpoint.accept().await {
        let token = token.clone();
        let events = events.clone();
        tokio::spawn(async move {
            match incoming.await {
                Ok(connection) => {
                    if let Err(error) = serve_connection(connection, &token, events).await {
                        eprintln!("attachment ended: {error:#}");
                    }
                }
                Err(error) => eprintln!("QUIC handshake failed: {error}"),
            }
        });
    }
}

async fn serve_connection(
    connection: Connection,
    token: &str,
    events: mpsc::Sender<Event>,
) -> Result<()> {
    let (mut writer, mut reader) = connection.accept_bi().await?;
    let hello = tokio::time::timeout(
        std::time::Duration::from_secs(10),
        read_frame_limited::<_, ClientFrame>(&mut reader, MAX_AUTH_FRAME_BYTES),
    )
    .await??;
    let (presented, client, size, after) = match hello {
        ClientFrame::Hello {
            token,
            client,
            size,
            after,
        } => (token, client, size, after),
        ClientFrame::Logs { token: presented } => {
            check_token(&presented, token)?;
            return send_logs_snapshot(writer, &events).await;
        }
        _ => bail!("first frame must authenticate the client"),
    };
    check_token(&presented, token)?;
    uuid::Uuid::parse_str(&client).context("invalid client identity")?;
    size.validate()?;
    let (sender, mut outgoing) = mpsc::channel(128);
    let attachment = uuid::Uuid::new_v4().to_string();
    events
        .send(Event::Join {
            id: client.clone(),
            connection: attachment.clone(),
            size,
            after,
            sender,
        })
        .await?;
    let receive = receive_input(reader, events.clone(), client.clone(), attachment.clone());
    let transmit = async {
        while let Some(frame) = outgoing.recv().await {
            send_frame(&mut writer, &frame).await?;
        }
        Ok::<(), anyhow::Error>(())
    };
    let result = tokio::select! { result = receive => result, result = transmit => result };
    if events
        .send(Event::Leave {
            id: client,
            connection: attachment,
        })
        .await
        .is_err()
    {
        eprintln!("session actor stopped before attachment cleanup");
    }
    connection.close(0u32.into(), b"attachment closed");
    result
}

/// Answer a one-shot logs request; the requester never joins the session.
async fn send_logs_snapshot(
    mut writer: quinn::SendStream,
    events: &mpsc::Sender<Event>,
) -> Result<()> {
    let (reply, snapshot) = tokio::sync::oneshot::channel();
    events.send(Event::Logs { reply }).await?;
    let update = snapshot.await.context("session stopped before logs")?;
    send_frame(&mut writer, &ServerFrame::Display { update }).await?;
    writer.finish()?;
    writer.stopped().await?;
    Ok(())
}

fn check_token(presented: &str, token: &str) -> Result<()> {
    ensure!(
        bool::from(presented.as_bytes().ct_eq(token.as_bytes())),
        "invalid attachment token"
    );
    Ok(())
}

async fn receive_input(
    mut reader: quinn::RecvStream,
    events: mpsc::Sender<Event>,
    id: String,
    connection: String,
) -> Result<()> {
    loop {
        let frame = read_frame(&mut reader).await?;
        events
            .send(Event::Frame {
                id: id.clone(),
                connection: connection.clone(),
                frame,
            })
            .await?;
    }
}

impl Session {
    async fn run(
        &mut self,
        mut output: mpsc::Receiver<PtyEvent>,
        mut events: mpsc::Receiver<Event>,
    ) -> Result<()> {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        let mut interrupt =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
        let mut tick = tokio::time::interval(std::time::Duration::from_millis(100));
        let mut output_open = true;
        loop {
            tokio::select! {
                event = output.recv(), if output_open => match event {
                    Some(PtyEvent::Output(bytes)) => self.publish_output(&bytes)?,
                    Some(PtyEvent::Failed(error)) => bail!("read Pi PTY: {error}"),
                    Some(PtyEvent::Closed) | None => output_open = false,
                },
                event = events.recv() => if let Some(event) = event { self.dispatch(event)?; },
                _ = tick.tick() => self.poll_exit()?,
                _ = terminate.recv() => return Ok(()),
                _ = interrupt.recv() => return Ok(()),
            }
        }
    }

    fn publish_output(&mut self, bytes: &[u8]) -> Result<()> {
        let update = self.terminal.feed(bytes)?;
        let responses = self.terminal.drain_responses();
        if !responses.is_empty() {
            self.pty.write_input(&responses)?;
        }
        self.broadcast(ServerFrame::Display { update });
        Ok(())
    }

    fn broadcast(&mut self, frame: ServerFrame) {
        self.peers
            .retain(|_, peer| !peer.ready || peer.sender.try_send(frame.clone()).is_ok());
    }

    fn poll_exit(&mut self) -> Result<()> {
        if self.exit_code.is_some() {
            return Ok(());
        }
        if let Some(status) = self.pty.child.try_wait()? {
            let code = status.code().unwrap_or(128);
            self.exit_code = Some(code);
            let path = self.state.join("runtime.json");
            let mut runtime: serde_json::Value = serde_json::from_slice(&std::fs::read(&path)?)?;
            runtime["exit_code"] = json!(code);
            write_private_json(&path, &runtime)?;
            self.broadcast(ServerFrame::Exit { code });
        }
        Ok(())
    }

    fn dispatch(&mut self, event: Event) -> Result<()> {
        match event {
            Event::Join {
                id,
                connection,
                size,
                after,
                sender,
            } => {
                if let Err(error) = self.join(id.clone(), connection, size, after, sender) {
                    eprintln!("reject attachment: {error:#}");
                    self.peers.remove(&id);
                    self.resize_to_smallest()?;
                }
                Ok(())
            }
            Event::Logs { reply } => {
                // The requester may already be gone; nothing else depends on delivery.
                let _ = reply.send(self.terminal.snapshot(None));
                Ok(())
            }
            Event::Leave { id, connection } => {
                if self
                    .peers
                    .get(&id)
                    .is_some_and(|peer| peer.connection == connection)
                {
                    self.peers.remove(&id);
                }
                self.resize_to_smallest()
            }
            Event::Frame {
                id,
                connection,
                frame,
            } => {
                if !self
                    .peers
                    .get(&id)
                    .is_some_and(|peer| peer.connection == connection)
                {
                    return Ok(());
                }
                if let Err(error) = self.apply_input(&id, frame) {
                    if let Some(peer) = self.peers.get(&id) {
                        if peer
                            .sender
                            .try_send(ServerFrame::Error {
                                message: format!("{error:#}"),
                            })
                            .is_err()
                        {
                            self.peers.remove(&id);
                        }
                    }
                }
                Ok(())
            }
        }
    }

    fn join(
        &mut self,
        id: String,
        connection: String,
        size: Size,
        after: Option<u64>,
        sender: mpsc::Sender<ServerFrame>,
    ) -> Result<()> {
        self.peers.insert(
            id.clone(),
            Peer {
                connection,
                ready: false,
                size,
                sender,
            },
        );
        self.resize_to_smallest()?;
        let update = self.terminal.snapshot(after);
        if let Some(peer) = self.peers.get_mut(&id) {
            peer.sender
                .try_send(ServerFrame::Display { update })
                .context("send initial replay")?;
            if let Some(code) = self.exit_code {
                peer.sender.try_send(ServerFrame::Exit { code })?;
            }
            peer.ready = true;
        }
        Ok(())
    }

    fn resize_to_smallest(&mut self) -> Result<()> {
        let size = self
            .peers
            .values()
            .fold(None, |minimum: Option<Size>, peer| {
                Some(match minimum {
                    None => peer.size,
                    Some(size) => Size {
                        rows: size.rows.min(peer.size.rows),
                        cols: size.cols.min(peer.size.cols),
                    },
                })
            });
        let Some(size) = size else {
            return Ok(());
        };
        if size == self.size {
            return Ok(());
        }
        self.pty.resize(size)?;
        self.size = size;
        let update = self.terminal.resize(size)?;
        self.broadcast(ServerFrame::Display { update });
        Ok(())
    }

    fn apply_input(&mut self, id: &str, frame: ClientFrame) -> Result<()> {
        match frame {
            ClientFrame::Input { sequence, data } => {
                ensure!(data.len() <= 1024 * 1024, "input exceeds one MiB");
                self.apply_sequenced_input(id, sequence, &data)
            }
            ClientFrame::Image { sequence, png } => {
                if self
                    .input_positions
                    .get(id)
                    .is_some_and(|seen| sequence <= *seen)
                {
                    return self.ack_input(id, sequence);
                }
                let path = write_image(&self.state, &png)?;
                let input = format!("\x1b[200~{}\x1b[201~", path.display());
                self.apply_sequenced_input(id, sequence, input.as_bytes())
            }
            ClientFrame::Resize { size } => {
                self.peers.get_mut(id).context("unknown client")?.size = size.validate()?;
                self.resize_to_smallest()
            }
            ClientFrame::Ack { .. } => Ok(()),
            ClientFrame::Detach => {
                self.peers.remove(id);
                self.resize_to_smallest()
            }
            ClientFrame::Hello { .. } | ClientFrame::Logs { .. } => {
                bail!("duplicate authentication frame")
            }
        }
    }

    fn apply_sequenced_input(&mut self, id: &str, sequence: u64, data: &[u8]) -> Result<()> {
        let previous = self.input_positions.get(id).copied().unwrap_or(0);
        if sequence <= previous {
            return self.ack_input(id, sequence);
        }
        ensure!(sequence == previous + 1, "input sequence has a gap");
        ensure!(self.exit_code.is_none(), "Pi has exited");
        self.pty.write_input(data)?;
        self.input_positions.insert(id.into(), sequence);
        self.ack_input(id, sequence)
    }

    fn ack_input(&self, id: &str, sequence: u64) -> Result<()> {
        self.peers
            .get(id)
            .context("unknown client")?
            .sender
            .try_send(ServerFrame::InputAck { sequence })
            .context("input acknowledgement queue full")
    }
}

#[cfg(test)]
#[path = "server_tests.rs"]
mod tests;

fn write_image(state: &Path, png: &[u8]) -> Result<PathBuf> {
    ensure!(
        png.starts_with(b"\x89PNG\r\n\x1a\n"),
        "clipboard is not a PNG image"
    );
    ensure!(
        png.len() <= MAX_IMAGE_BYTES,
        "clipboard image exceeds {MAX_IMAGE_BYTES} bytes"
    );
    let directory = state.join("images");
    std::fs::create_dir_all(&directory)?;
    std::fs::set_permissions(
        &directory,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )?;
    let path = directory.join(format!("pi-clipboard-{}.png", uuid::Uuid::new_v4()));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)?;
    file.write_all(png)?;
    Ok(path)
}

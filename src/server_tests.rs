use super::*;
use crate::terminal::DisplayRenderer;
use std::num::NonZeroU16;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;
use tokio::io::AsyncWriteExt;

struct Fixture {
    root: tempfile::TempDir,
    task: tokio::task::JoinHandle<Result<()>>,
    ticket: Ticket,
    child: u32,
}

impl Fixture {
    async fn start() -> Self {
        let root = tempfile::tempdir().unwrap();
        let pi = root.path().join("pi");
        std::fs::write(&pi, "#!/usr/bin/python3\nimport sys,tty,time\ntime.sleep(.05)\ntty.setraw(0)\nprint('READY',flush=True)\nfor command in sys.stdin:\n if command.strip()=='bulk':\n  for i in range(80): print(f'BULK_LINE_{i:03}\\r',flush=True)\n if command.strip()=='offline':\n  time.sleep(.2)\n  print('OFFLINE_REPLY\\r',flush=True)\n").unwrap();
        std::fs::set_permissions(&pi, std::fs::Permissions::from_mode(0o700)).unwrap();
        let launch = Launch {
            name: "test".into(),
            project: "fixture".into(),
            cwd: root.path().into(),
            pi,
            conversation: uuid::Uuid::new_v4().to_string(),
            resume: false,
            environment: std::collections::BTreeMap::new(),
            prompt: None,
            size: Size {
                rows: 46,
                cols: 165,
            },
            history_bytes: DEFAULT_HISTORY_BYTES,
        };
        write_private_json(&root.path().join("launch.json"), &launch).unwrap();
        let state = root.path().to_owned();
        let task = tokio::spawn(serve(state));
        let runtime_path = root.path().join("runtime.json");
        for _ in 0..200 {
            if runtime_path.exists() {
                break;
            }
            assert!(
                !task.is_finished(),
                "server failed before publishing runtime"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        let runtime: Runtime =
            serde_json::from_slice(&std::fs::read(runtime_path).unwrap()).unwrap();
        let ticket = Ticket {
            address: runtime.address,
            certificate: runtime.certificate,
            token: runtime.token,
        };
        Self {
            root,
            task,
            ticket,
            child: runtime.child_pid.unwrap(),
        }
    }

    async fn attach(
        &self,
        id: &str,
        size: Size,
        after: Option<u64>,
    ) -> (Endpoint, Connection, quinn::SendStream, quinn::RecvStream) {
        let (endpoint, connection) = crate::client::connect(&self.ticket, "127.0.0.1")
            .await
            .unwrap();
        let (mut writer, reader) = connection.open_bi().await.unwrap();
        send_frame(
            &mut writer,
            &ClientFrame::Hello {
                token: self.ticket.token.clone(),
                client: id.into(),
                size,
                after,
            },
        )
        .await
        .unwrap();
        (endpoint, connection, writer, reader)
    }

    async fn stop(self) {
        self.task.abort();
        assert!(self.task.await.unwrap_err().is_cancelled());
        assert_eq!(
            unsafe { libc::kill(self.child as i32, 0) },
            -1,
            "owned PTY child leaked"
        );
    }
}

async fn next_display(reader: &mut quinn::RecvStream) -> DisplayUpdate {
    loop {
        let frame: ServerFrame = tokio::time::timeout(Duration::from_secs(3), read_frame(reader))
            .await
            .unwrap()
            .unwrap();
        match frame {
            ServerFrame::Display { update } => return update,
            ServerFrame::Error { message } => panic!("server error: {message}"),
            _ => {}
        }
    }
}

#[derive(Default)]
struct Rows(Vec<String>);
impl vt100::Callbacks for Rows {
    fn on_scroll(&mut self, row: vt100::capture::RowContents<'_>, alternate: bool) {
        assert!(!alternate);
        let mut text = String::new();
        row.write_formatted_basic(&mut text, &mut Default::default())
            .unwrap();
        self.0.push(text);
    }
}

fn terminal_text(terminal: &vt100::Parser<Rows>) -> String {
    format!(
        "{}\n{}",
        terminal.callbacks().0.join("\n"),
        terminal.screen().contents()
    )
}

#[tokio::test]
async fn quic_bulk_output_and_reconnect_preserve_every_line_and_child() {
    let fixture = Fixture::start().await;
    let id = uuid::Uuid::new_v4().to_string();
    let size = Size {
        rows: 46,
        cols: 165,
    };
    let (_endpoint, connection, mut writer, mut reader) = fixture.attach(&id, size, None).await;
    let mut renderer = DisplayRenderer::new(size);
    let mut terminal = vt100::Parser::new_with_callbacks(
        NonZeroU16::new(46).unwrap(),
        NonZeroU16::new(165).unwrap(),
        0,
        Rows::default(),
    );
    let initial = next_display(&mut reader).await;
    terminal.process(&renderer.render(&initial).unwrap());
    while !terminal_text(&terminal).contains("READY") {
        let update = next_display(&mut reader).await;
        terminal.process(&renderer.render(&update).unwrap());
    }
    send_frame(
        &mut writer,
        &ClientFrame::Input {
            sequence: 1,
            data: b"bulk\n".to_vec(),
        },
    )
    .await
    .unwrap();
    let mut after;
    loop {
        let update = next_display(&mut reader).await;
        after = update.sequence;
        terminal.process(&renderer.render(&update).unwrap());
        if terminal_text(&terminal).contains("BULK_LINE_079") {
            break;
        }
    }
    let text = terminal_text(&terminal);
    for index in 0..80 {
        assert_eq!(
            text.matches(&format!("BULK_LINE_{index:03}")).count(),
            1,
            "marker{index} absent or duplicated"
        );
    }
    send_frame(
        &mut writer,
        &ClientFrame::Input {
            sequence: 2,
            data: b"offline\n".to_vec(),
        },
    )
    .await
    .unwrap();
    loop {
        let frame: ServerFrame =
            tokio::time::timeout(Duration::from_secs(3), read_frame(&mut reader))
                .await
                .unwrap()
                .unwrap();
        if matches!(frame, ServerFrame::InputAck { sequence: 2 }) {
            break;
        }
    }
    connection.close(0u32.into(), b"test network interruption");
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(unsafe { libc::kill(fixture.child as i32, 0) }, 0);
    let (_resumed_endpoint, resumed, mut resumed_writer, mut resumed_reader) =
        fixture.attach(&id, size, Some(after)).await;
    let update = next_display(&mut resumed_reader).await;
    assert!(!update.expired);
    terminal.process(&renderer.render(&update).unwrap());
    assert!(terminal_text(&terminal).contains("OFFLINE_REPLY"));
    for index in 0..80 {
        assert_eq!(
            terminal_text(&terminal)
                .matches(&format!("BULK_LINE_{index:03}"))
                .count(),
            1
        );
    }
    send_frame(&mut resumed_writer, &ClientFrame::Detach)
        .await
        .unwrap();
    resumed.close(0u32.into(), b"done");
    fixture.stop().await;
}

#[tokio::test]
async fn logs_request_returns_retained_output_without_joining_or_resizing() {
    let fixture = Fixture::start().await;
    let size = Size {
        rows: 46,
        cols: 165,
    };
    let id = uuid::Uuid::new_v4().to_string();
    let (_endpoint, connection, mut writer, mut reader) = fixture.attach(&id, size, None).await;
    let mut renderer = DisplayRenderer::new(size);
    let mut terminal = vt100::Parser::new_with_callbacks(
        NonZeroU16::new(46).unwrap(),
        NonZeroU16::new(165).unwrap(),
        0,
        Rows::default(),
    );
    while !terminal_text(&terminal).contains("READY") {
        terminal.process(&renderer.render(&next_display(&mut reader).await).unwrap());
    }
    send_frame(
        &mut writer,
        &ClientFrame::Input {
            sequence: 1,
            data: b"bulk\n".to_vec(),
        },
    )
    .await
    .unwrap();
    while !terminal_text(&terminal).contains("BULK_LINE_079") {
        terminal.process(&renderer.render(&next_display(&mut reader).await).unwrap());
    }
    let (_logs_endpoint, logs) = crate::client::connect(&fixture.ticket, "127.0.0.1")
        .await
        .unwrap();
    let (mut logs_writer, mut logs_reader) = logs.open_bi().await.unwrap();
    send_frame(
        &mut logs_writer,
        &ClientFrame::Logs {
            token: fixture.ticket.token.clone(),
        },
    )
    .await
    .unwrap();
    let snapshot = next_display(&mut logs_reader).await;
    assert_eq!(snapshot.size, size);
    let mut logged = vt100::Parser::new_with_callbacks(
        NonZeroU16::new(46).unwrap(),
        NonZeroU16::new(165).unwrap(),
        0,
        Rows::default(),
    );
    logged.process(&DisplayRenderer::new(size).render(&snapshot).unwrap());
    for index in 0..80 {
        assert_eq!(
            terminal_text(&logged)
                .matches(&format!("BULK_LINE_{index:03}"))
                .count(),
            1
        );
    }
    // A logs request is not an attachment: frames the attached client still
    // receives (queued PTY output) all keep its size.
    let deadline = tokio::time::Instant::now() + Duration::from_millis(300);
    while let Ok(frame) =
        tokio::time::timeout_at(deadline, read_frame::<_, ServerFrame>(&mut reader)).await
    {
        if let ServerFrame::Display { update } = frame.unwrap() {
            assert_eq!(update.size, size, "logs request resized the session");
        }
    }
    logs.close(0u32.into(), b"done");
    connection.close(0u32.into(), b"done");
    fixture.stop().await;
}

#[tokio::test]
async fn oversized_unauthenticated_frame_is_rejected_without_stopping_pi() {
    let fixture = Fixture::start().await;
    let (_endpoint, connection) = crate::client::connect(&fixture.ticket, "127.0.0.1")
        .await
        .unwrap();
    let (mut writer, mut reader) = connection.open_bi().await.unwrap();
    writer.write_u32(8193).await.unwrap();
    let result = tokio::time::timeout(
        Duration::from_secs(1),
        read_frame::<_, ServerFrame>(&mut reader),
    )
    .await;
    assert!(
        result
            .expect("oversized authentication header must be rejected before reading its body")
            .is_err()
    );
    assert!(!fixture.task.is_finished());
    assert_eq!(unsafe { libc::kill(fixture.child as i32, 0) }, 0);
    connection.close(0u32.into(), b"test complete");
    fixture.stop().await;
}

#[tokio::test]
async fn clients_share_smallest_size_and_images_are_unique_private_files() {
    let fixture = Fixture::start().await;
    let first = uuid::Uuid::new_v4().to_string();
    let (_endpoint, connection, mut writer, mut reader) = fixture
        .attach(
            &first,
            Size {
                rows: 46,
                cols: 165,
            },
            None,
        )
        .await;
    next_display(&mut reader).await;
    let second = uuid::Uuid::new_v4().to_string();
    let (_other_endpoint, other, mut other_writer, mut other_reader) = fixture
        .attach(&second, Size { rows: 20, cols: 80 }, None)
        .await;
    let update = next_display(&mut other_reader).await;
    assert_eq!(update.size, Size { rows: 20, cols: 80 });
    loop {
        if next_display(&mut reader).await.size == update.size {
            break;
        }
    }
    let png_a = b"\x89PNG\r\n\x1a\nfirst".to_vec();
    let png_b = b"\x89PNG\r\n\x1a\nsecond".to_vec();
    send_frame(
        &mut writer,
        &ClientFrame::Image {
            sequence: 1,
            png: png_a.clone(),
        },
    )
    .await
    .unwrap();
    send_frame(
        &mut other_writer,
        &ClientFrame::Image {
            sequence: 1,
            png: png_b.clone(),
        },
    )
    .await
    .unwrap();
    for reader in [&mut reader, &mut other_reader] {
        loop {
            let frame: ServerFrame =
                tokio::time::timeout(Duration::from_secs(3), read_frame(reader))
                    .await
                    .unwrap()
                    .unwrap();
            if matches!(frame, ServerFrame::InputAck { sequence: 1 }) {
                break;
            }
        }
    }
    let images: Vec<_> = std::fs::read_dir(fixture.root.path().join("images"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(images.len(), 2);
    let data: Vec<_> = images
        .iter()
        .map(|path| {
            assert_eq!(
                std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
                0o600
            );
            std::fs::read(path).unwrap()
        })
        .collect();
    assert!(data.contains(&png_a) && data.contains(&png_b));
    other.close(0u32.into(), b"smaller client leaves");
    loop {
        if next_display(&mut reader).await.size
            == (Size {
                rows: 46,
                cols: 165,
            })
        {
            break;
        }
    }
    connection.close(0u32.into(), b"done");
    fixture.stop().await;
}

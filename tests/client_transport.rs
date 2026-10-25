use anyhow::{Context, Result, ensure};
use quinn::Endpoint;
use remote_agent_rs::client;
use remote_agent_rs::protocol::{
    ClientFrame, DisplayUpdate, ServerFrame, Size, Ticket, read_frame, send_frame,
};
use rustls::pki_types::PrivatePkcs8KeyDer;
use std::time::Duration;

fn bind_test_peer() -> Result<(Endpoint, Ticket)> {
    let certificate = rcgen::generate_simple_self_signed(vec!["remote-agent".into()])?;
    let der = certificate.cert.der().clone();
    let key = PrivatePkcs8KeyDer::from(certificate.key_pair.serialize_der());
    let config = quinn::ServerConfig::with_single_cert(vec![der.clone()], key.into())?;
    let endpoint = Endpoint::server(config, "127.0.0.1:0".parse()?)?;
    let ticket = Ticket {
        address: endpoint.local_addr()?.to_string(),
        certificate: der.to_vec(),
        token: "owned-loopback-ticket".into(),
    };
    Ok((endpoint, ticket))
}

async fn serve_test_peer(endpoint: &Endpoint) -> Result<()> {
    let incoming = endpoint.accept().await.context("accept loopback client")?;
    let connection = incoming.await?;
    let (mut writer, mut reader) = connection.accept_bi().await?;
    let hello: ClientFrame = read_frame(&mut reader).await?;
    ensure!(matches!(hello, ClientFrame::Hello { token, .. } if token == "owned-loopback-ticket"));
    send_frame(
        &mut writer,
        &ServerFrame::Display {
            update: DisplayUpdate {
                sequence: 7,
                size: Size { rows: 24, cols: 80 },
                scrolled: vec![b"history\r\n".to_vec()],
                screen: b"native client output".to_vec(),
                control: Vec::new(),
                reset: true,
                expired: false,
            },
        },
    )
    .await?;
    let ack: ClientFrame = read_frame(&mut reader).await?;
    ensure!(matches!(ack, ClientFrame::Ack { sequence: 7 }));
    send_frame(&mut writer, &ServerFrame::InputAck { sequence: 11 }).await?;
    let detached: ClientFrame = read_frame(&mut reader).await?;
    ensure!(matches!(detached, ClientFrame::Detach));
    send_frame(&mut writer, &ServerFrame::Exit { code: 0 }).await?;
    connection.closed().await;
    Ok(())
}

#[tokio::test]
async fn production_client_exchanges_output_and_ack_with_quic_peer() -> Result<()> {
    tokio::time::timeout(Duration::from_secs(15), async {
        let (peer, ticket) = bind_test_peer()?;
        let exchange = async {
            let (_endpoint, connection) = client::connect(&ticket, "127.0.0.1").await?;
            let (mut writer, mut reader) = connection.open_bi().await?;
            send_frame(
                &mut writer,
                &ClientFrame::Hello {
                    token: ticket.token.clone(),
                    client: "native-test".into(),
                    size: Size { rows: 24, cols: 80 },
                    after: None,
                },
            )
            .await?;
            let output: ServerFrame = read_frame(&mut reader).await?;
            let ServerFrame::Display { update } = output else {
                anyhow::bail!("expected display output");
            };
            assert_eq!(update.sequence, 7);
            assert_eq!(update.screen, b"native client output");
            assert_eq!(update.scrolled, vec![b"history\r\n".to_vec()]);
            send_frame(&mut writer, &ClientFrame::Ack { sequence: 7 }).await?;
            let ack: ServerFrame = read_frame(&mut reader).await?;
            ensure!(matches!(ack, ServerFrame::InputAck { sequence: 11 }));
            send_frame(&mut writer, &ClientFrame::Detach).await?;
            let exit: ServerFrame = read_frame(&mut reader).await?;
            ensure!(matches!(exit, ServerFrame::Exit { code: 0 }));
            connection.close(0u32.into(), b"test complete");
            Ok::<_, anyhow::Error>(())
        };
        tokio::try_join!(serve_test_peer(&peer), exchange)?;
        Ok::<_, anyhow::Error>(())
    })
    .await?
}

#[tokio::test]
async fn production_client_rejects_a_different_ticket_certificate() -> Result<()> {
    tokio::time::timeout(Duration::from_secs(15), async {
        let (peer, mut ticket) = bind_test_peer()?;
        let unrelated = rcgen::generate_simple_self_signed(vec!["remote-agent".into()])?;
        ticket.certificate = unrelated.cert.der().to_vec();
        let accept = async {
            let incoming = peer.accept().await.context("accept untrusted client")?;
            ensure!(incoming.await.is_err(), "peer unexpectedly accepted client");
            Ok::<_, anyhow::Error>(())
        };
        let connect = async {
            ensure!(
                client::connect(&ticket, "127.0.0.1").await.is_err(),
                "client unexpectedly trusted a different certificate"
            );
            Ok::<_, anyhow::Error>(())
        };
        tokio::try_join!(accept, connect)?;
        Ok::<_, anyhow::Error>(())
    })
    .await?
}

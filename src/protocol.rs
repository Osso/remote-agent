use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

// Includes base64 expansion and JSON metadata for large display frames.
pub const MAX_FRAME_BYTES: usize = 80 * 1024 * 1024;
pub const MAX_AUTH_FRAME_BYTES: usize = 8192;
pub const DEFAULT_HISTORY_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_IMAGE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Size {
    pub rows: u16,
    pub cols: u16,
}

impl Size {
    pub fn validate(self) -> Result<Self> {
        ensure!(
            self.rows > 0 && self.cols > 0,
            "terminal dimensions must be positive"
        );
        ensure!(
            self.rows <= 1000 && self.cols <= 1000,
            "terminal dimensions exceed 1000 cells"
        );
        Ok(self)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DisplayUpdate {
    pub sequence: u64,
    pub size: Size,
    #[serde(with = "base64_rows")]
    pub scrolled: Vec<Vec<u8>>,
    #[serde(with = "base64_bytes")]
    pub screen: Vec<u8>,
    #[serde(with = "base64_bytes")]
    pub control: Vec<u8>,
    pub reset: bool,
    pub expired: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientFrame {
    Hello {
        token: String,
        client: String,
        size: Size,
        after: Option<u64>,
    },
    Input {
        sequence: u64,
        #[serde(with = "base64_bytes")]
        data: Vec<u8>,
    },
    Resize {
        size: Size,
    },
    Image {
        sequence: u64,
        #[serde(with = "base64_bytes")]
        png: Vec<u8>,
    },
    Ack {
        sequence: u64,
    },
    Detach,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerFrame {
    Display { update: DisplayUpdate },
    InputAck { sequence: u64 },
    Exit { code: i32 },
    Error { message: String },
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Ticket {
    pub address: String,
    #[serde(with = "base64_bytes")]
    pub certificate: Vec<u8>,
    pub token: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Launch {
    pub name: String,
    pub project: String,
    pub cwd: std::path::PathBuf,
    pub pi: std::path::PathBuf,
    pub conversation: String,
    pub resume: bool,
    pub environment: std::collections::BTreeMap<String, String>,
    pub prompt: Option<String>,
    pub size: Size,
    pub history_bytes: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum ControlRequest {
    Start {
        host: String,
        project: String,
        name: Option<String>,
        resume: Option<String>,
        prompt: Option<String>,
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
}

pub async fn send_frame<W: AsyncWriteExt + Unpin, T: Serialize>(
    writer: &mut W,
    frame: &T,
) -> Result<()> {
    let data = serde_json::to_vec(frame)?;
    ensure!(
        data.len() <= MAX_FRAME_BYTES,
        "protocol frame exceeds size limit"
    );
    writer.write_u32(data.len() as u32).await?;
    writer.write_all(&data).await?;
    writer.flush().await?;
    Ok(())
}

pub async fn read_frame<R: AsyncReadExt + Unpin, T: serde::de::DeserializeOwned>(
    reader: &mut R,
) -> Result<T> {
    read_frame_limited(reader, MAX_FRAME_BYTES).await
}

pub async fn read_frame_limited<R: AsyncReadExt + Unpin, T: serde::de::DeserializeOwned>(
    reader: &mut R,
    limit: usize,
) -> Result<T> {
    let length = reader.read_u32().await? as usize;
    ensure!(length <= limit, "protocol frame exceeds size limit");
    let mut data = vec![0; length];
    reader.read_exact(&mut data).await?;
    Ok(serde_json::from_slice(&data)?)
}

mod base64_bytes {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use serde::{Deserialize, Deserializer, Serializer, de::Error};

    pub fn serialize<S: Serializer>(bytes: &[u8], serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&STANDARD.encode(bytes))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Vec<u8>, D::Error> {
        let encoded = String::deserialize(deserializer)?;
        STANDARD.decode(encoded).map_err(D::Error::custom)
    }
}

mod base64_rows {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use serde::{Deserialize, Deserializer, Serializer, de::Error, ser::SerializeSeq};

    pub fn serialize<S: Serializer>(rows: &[Vec<u8>], serializer: S) -> Result<S::Ok, S::Error> {
        let mut sequence = serializer.serialize_seq(Some(rows.len()))?;
        for row in rows {
            sequence.serialize_element(&STANDARD.encode(row))?;
        }
        sequence.end()
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Vec<Vec<u8>>, D::Error> {
        Vec::<String>::deserialize(deserializer)?
            .into_iter()
            .map(|encoded| STANDARD.decode(encoded).map_err(D::Error::custom))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, to_value};

    #[tokio::test]
    async fn authentication_limit_rejects_header_without_waiting_for_payload() {
        let (mut writer, mut reader) = tokio::io::duplex(8);
        writer.write_u32(8193).await.unwrap();
        let result = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            read_frame_limited::<_, ClientFrame>(&mut reader, 8192),
        )
        .await;
        assert!(
            result
                .unwrap()
                .unwrap_err()
                .to_string()
                .contains("size limit")
        );
    }

    #[test]
    fn display_bytes_are_base64_and_round_trip_without_parsing_fragments() {
        let update = DisplayUpdate {
            sequence: 12,
            size: Size { rows: 24, cols: 80 },
            scrolled: vec![vec![0xf0, 0x9f], vec![0x98, 0x80, 0x1b, b'[']],
            screen: vec![0, 0xff, b'J'],
            control: vec![0x1b, b']', 7],
            reset: true,
            expired: false,
        };
        let value = to_value(&update).unwrap();
        assert_eq!(value["screen"], "AP9K");
        assert_eq!(value["control"], "G10H");
        assert_eq!(value["scrolled"], json!(["8J8=", "mIAbWw=="]));
        let decoded: DisplayUpdate = serde_json::from_value(value).unwrap();
        assert_eq!(decoded.scrolled, update.scrolled);
        assert_eq!(decoded.screen, update.screen);
        assert_eq!(decoded.control, update.control);
    }

    #[test]
    fn client_and_ticket_bytes_use_strict_base64() {
        for frame in [
            ClientFrame::Input {
                sequence: 3,
                data: vec![0, 255],
            },
            ClientFrame::Image {
                sequence: 4,
                png: vec![0, 255],
            },
        ] {
            let value = to_value(&frame).unwrap();
            let field = if matches!(frame, ClientFrame::Input { .. }) {
                "data"
            } else {
                "png"
            };
            assert_eq!(value[field], "AP8=");
            let decoded: ClientFrame = serde_json::from_value(value.clone()).unwrap();
            assert_eq!(to_value(decoded).unwrap(), value);
            let mut numeric = value.clone();
            numeric[field] = json!([0, 255]);
            assert!(serde_json::from_value::<ClientFrame>(numeric).is_err());
            let mut invalid = value;
            invalid[field] = json!("%%%!");
            assert!(serde_json::from_value::<ClientFrame>(invalid).is_err());
        }
        let ticket = Ticket {
            address: "localhost:1".into(),
            certificate: vec![0, 255],
            token: "secret".into(),
        };
        let value = to_value(&ticket).unwrap();
        assert_eq!(value["certificate"], "AP8=");
        let decoded: Ticket = serde_json::from_value(value).unwrap();
        assert_eq!(decoded.certificate, ticket.certificate);
    }
}

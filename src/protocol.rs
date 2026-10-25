use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

pub const MAX_FRAME_BYTES: usize = 32 * 1024 * 1024;
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
    pub scrolled: Vec<Vec<u8>>,
    pub screen: Vec<u8>,
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
        data: Vec<u8>,
    },
    Resize {
        size: Size,
    },
    Image {
        sequence: u64,
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
    pub prompt: Option<String>,
    pub size: Size,
    pub history_bytes: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum ControlRequest {
    Start {
        project: String,
        name: Option<String>,
        resume: Option<String>,
        prompt_file: Option<String>,
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
    let length = reader.read_u32().await? as usize;
    ensure!(
        length <= MAX_FRAME_BYTES,
        "protocol frame exceeds size limit"
    );
    let mut data = vec![0; length];
    reader.read_exact(&mut data).await?;
    Ok(serde_json::from_slice(&data)?)
}

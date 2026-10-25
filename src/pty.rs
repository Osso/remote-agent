use crate::protocol::{Launch, Size};
use anyhow::{Context, Result, bail};
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::process::CommandExt;
use std::process::{Child, Command};
use tokio::sync::mpsc;

pub enum PtyEvent {
    Output(Vec<u8>),
    Closed,
    Failed(String),
}

pub struct Pty {
    master: File,
    pub child: Child,
}

impl Pty {
    pub fn spawn(launch: &Launch, events: mpsc::Sender<PtyEvent>) -> Result<Self> {
        let (master, slave) = open_pty(launch.size)?;
        let child = spawn_pi_child(launch, slave)?;
        spawn_reader(master.try_clone()?, events);
        Ok(Self { master, child })
    }

    pub fn write_input(&mut self, bytes: &[u8]) -> Result<()> {
        self.master
            .write_all(bytes)
            .context("write input to session PTY")
    }

    pub fn resize(&self, size: Size) -> Result<()> {
        let dimensions = winsize(size.validate()?);
        // master is an owned live PTY descriptor; dimensions has the ioctl layout.
        if unsafe { libc::ioctl(self.master.as_raw_fd(), libc::TIOCSWINSZ, &dimensions) } < 0 {
            bail!("resize PTY: {}", std::io::Error::last_os_error());
        }
        Ok(())
    }

    pub fn terminate(&mut self) -> Result<()> {
        if self.child.try_wait()?.is_none() {
            // The child created its own session, so this targets only its process group.
            let result = unsafe { libc::kill(-(self.child.id() as i32), libc::SIGTERM) };
            if result < 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
                bail!(
                    "terminate Pi process group: {}",
                    std::io::Error::last_os_error()
                );
            }
        }
        Ok(())
    }
}

fn build_pi_command(launch: &Launch) -> Command {
    let mut command = Command::new(&launch.pi);
    let session_flag = if launch.resume {
        "--session"
    } else {
        "--session-id"
    };
    command.arg(session_flag).arg(&launch.conversation);
    if let Some(prompt) = &launch.prompt {
        command.arg(prompt);
    }
    command
        .current_dir(&launch.cwd)
        .env("TERM", "xterm-256color")
        .env("COLORTERM", "truecolor")
        .env("TERM_PROGRAM", "remote-agent-rs")
        .env("TERM_PROGRAM_VERSION", env!("CARGO_PKG_VERSION"));
    // Pi must detect the virtual endpoint, not an inherited physical GUI terminal.
    for name in [
        "GHOSTTY_RESOURCES_DIR",
        "KITTY_WINDOW_ID",
        "WEZTERM_PANE",
        "WARP_SESSION_ID",
        "WARP_TERMINAL_SESSION_UUID",
        "ITERM_SESSION_ID",
        "WT_SESSION",
        "TERMINAL_EMULATOR",
        "TMUX",
        "STY",
    ] {
        command.env_remove(name);
    }
    command
}

fn spawn_pi_child(launch: &Launch, slave: File) -> Result<Child> {
    let mut command = build_pi_command(launch);
    command
        .stdin(slave.try_clone()?)
        .stdout(slave.try_clone()?)
        .stderr(slave);
    // Only async-signal-safe libc operations run between fork and exec.
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() < 0 || libc::ioctl(0, libc::TIOCSCTTY, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    command
        .spawn()
        .with_context(|| format!("launch Pi at {}", launch.pi.display()))
}

impl Drop for Pty {
    fn drop(&mut self) {
        if let Err(error) = self.terminate() {
            eprintln!("stop owned Pi process: {error:#}");
        }
        for _ in 0..200 {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => std::thread::sleep(std::time::Duration::from_millis(10)),
                Err(error) => {
                    eprintln!("reap owned Pi process: {error}");
                    return;
                }
            }
        }
        // Only this PTY's process group is escalated after graceful shutdown expires.
        let killed = unsafe { libc::kill(-(self.child.id() as i32), libc::SIGKILL) };
        if killed < 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH) {
            eprintln!(
                "kill owned Pi process group: {}",
                std::io::Error::last_os_error()
            );
        }
        if let Err(error) = self.child.wait() {
            eprintln!("reap killed Pi process: {error}");
        }
    }
}

fn winsize(size: Size) -> libc::winsize {
    libc::winsize {
        ws_row: size.rows,
        ws_col: size.cols,
        ws_xpixel: 0,
        ws_ypixel: 0,
    }
}

fn open_pty(size: Size) -> Result<(File, File)> {
    let mut master = -1;
    let mut slave = -1;
    let size = winsize(size.validate()?);
    // openpty initializes both descriptors on success; File takes ownership exactly once.
    let result = unsafe {
        libc::openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null(),
            &size,
        )
    };
    if result < 0 {
        bail!("open session PTY: {}", std::io::Error::last_os_error());
    }
    let pair = unsafe { (File::from_raw_fd(master), File::from_raw_fd(slave)) };
    for file in [&pair.0, &pair.1] {
        // Keep the master and incidental slave duplicates out of the executed child.
        if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
            bail!("set PTY close-on-exec: {}", std::io::Error::last_os_error());
        }
    }
    Ok(pair)
}

fn spawn_reader(mut master: File, events: mpsc::Sender<PtyEvent>) {
    std::thread::spawn(move || {
        let mut buffer = [0; 8192];
        loop {
            let event = match master.read(&mut buffer) {
                Ok(0) => PtyEvent::Closed,
                Ok(count) => PtyEvent::Output(buffer[..count].to_vec()),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) if error.raw_os_error() == Some(libc::EIO) => PtyEvent::Closed,
                Err(error) => PtyEvent::Failed(error.to_string()),
            };
            let ended = !matches!(event, PtyEvent::Output(_));
            if events.blocking_send(event).is_err() || ended {
                break;
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn native_pty_retains_bulk_output_and_applies_dimensions() {
        let (master, mut slave) = open_pty(Size {
            rows: 46,
            cols: 165,
        })
        .unwrap();
        let mut dimensions: libc::winsize = unsafe { std::mem::zeroed() };
        assert_eq!(
            unsafe { libc::ioctl(slave.as_raw_fd(), libc::TIOCGWINSZ, &mut dimensions) },
            0
        );
        assert_eq!((dimensions.ws_row, dimensions.ws_col), (46, 165));
        let (sender, mut receiver) = mpsc::channel(64);
        spawn_reader(master, sender);
        let text: String = (0..80).map(|i| format!("BULK_{i:03}\n")).collect();
        slave.write_all(text.as_bytes()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut output = Vec::new();
        while !String::from_utf8_lossy(&output).contains("BULK_079") {
            assert!(Instant::now() < deadline, "PTY bulk output timed out");
            if let Ok(PtyEvent::Output(bytes)) = receiver.try_recv() {
                output.extend(bytes);
            }
            std::thread::sleep(Duration::from_millis(2));
        }
        for i in 0..80 {
            assert!(String::from_utf8_lossy(&output).contains(&format!("BULK_{i:03}")));
        }
    }
}

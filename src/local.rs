//! Local terminal input and one-shot clipboard acquisition. No remote state or key hooks.
use crate::protocol::{MAX_IMAGE_BYTES, Size};
use anyhow::{Context, Result, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use std::{
    ffi::OsStr,
    io::{Read, Write},
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
    path::Path,
    process::{Command, Stdio},
};

#[derive(Debug, PartialEq, Eq)]
pub enum InputAction {
    Bytes(Vec<u8>),
    PasteImage,
    Detach,
}

/// Retains only an incomplete escape token, not pasted content. Call `finish`
/// after an escape timeout or EOF to forward a standalone Escape unchanged.
#[derive(Default)]
pub struct InputDecoder {
    pending: Vec<u8>,
    bracketed_paste: bool,
}

const PASTE_START: &[u8] = b"\x1b[200~";
const PASTE_END: &[u8] = b"\x1b[201~";
const MAX_ESCAPE_BYTES: usize = 128;

impl InputDecoder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn feed(&mut self, input: &[u8]) -> Vec<InputAction> {
        let mut actions = Vec::new();
        let mut bytes = Vec::new();
        for &byte in input {
            if self.bracketed_paste {
                self.feed_paste(byte, &mut bytes);
            } else {
                self.feed_key(byte, &mut bytes, &mut actions);
            }
        }
        emit_bytes(&mut bytes, &mut actions);
        actions
    }

    /// Flushes incomplete tokens without leaving bracketed-paste mode.
    pub fn finish(&mut self) -> Vec<InputAction> {
        let mut actions = Vec::new();
        emit_bytes(&mut self.pending, &mut actions);
        actions
    }

    fn feed_paste(&mut self, byte: u8, bytes: &mut Vec<u8>) {
        self.pending.push(byte);
        while !PASTE_END.starts_with(&self.pending) {
            bytes.push(self.pending.remove(0));
        }
        if self.pending == PASTE_END {
            bytes.append(&mut self.pending);
            self.bracketed_paste = false;
        }
    }

    fn feed_key(&mut self, byte: u8, bytes: &mut Vec<u8>, actions: &mut Vec<InputAction>) {
        if self.pending == b"\x1b" && byte != b'[' {
            bytes.append(&mut self.pending);
        }
        if self.pending.is_empty() {
            match byte {
                0x1b => self.pending.push(byte),
                0x16 | 0x1c => {
                    emit_bytes(bytes, actions);
                    actions.push(if byte == 0x16 {
                        InputAction::PasteImage
                    } else {
                        InputAction::Detach
                    });
                }
                _ => bytes.push(byte),
            }
            return;
        }
        self.pending.push(byte);
        if self.pending.len() == 2 {
            return;
        }
        if (0x40..=0x7e).contains(&byte) {
            if self.pending == PASTE_START {
                self.bracketed_paste = true;
            } else if let Some(action) = control_shortcut(&self.pending) {
                emit_bytes(bytes, actions);
                actions.push(action);
                self.pending.clear();
                return;
            }
            bytes.append(&mut self.pending);
        } else if self.pending.len() >= MAX_ESCAPE_BYTES {
            bytes.append(&mut self.pending);
        }
    }
}

fn emit_bytes(bytes: &mut Vec<u8>, actions: &mut Vec<InputAction>) {
    if !bytes.is_empty() {
        actions.push(InputAction::Bytes(std::mem::take(bytes)));
    }
}

fn control_shortcut(sequence: &[u8]) -> Option<InputAction> {
    if matches!(sequence, b"\x1b[27;5;118~" | b"\x1b[27;5;86~")
        || is_kitty_control_key(sequence, &["118", "86"])
    {
        Some(InputAction::PasteImage)
    } else if sequence == b"\x1b[27;5;92~" || is_kitty_control_key(sequence, &["92"]) {
        Some(InputAction::Detach)
    } else {
        None
    }
}

fn is_kitty_control_key(sequence: &[u8], keys: &[&str]) -> bool {
    let Some(body) = sequence
        .strip_prefix(b"\x1b[")
        .and_then(|s| s.strip_suffix(b"u"))
    else {
        return false;
    };
    let Ok(body) = std::str::from_utf8(body) else {
        return false;
    };
    let Some((key, modifiers)) = body.split_once(';') else {
        return false;
    };
    let mut key_fields = key.split(':');
    if !key_fields.next().is_some_and(|key| keys.contains(&key)) {
        return false;
    }
    let alternatives: Vec<_> = key_fields.collect();
    if alternatives.len() > 2
        || alternatives
            .iter()
            .any(|s| !s.is_empty() && s.parse::<u32>().is_err())
    {
        return false;
    }
    let (modifiers, event) = modifiers.split_once(':').unwrap_or((modifiers, "1"));
    if !matches!(event, "1" | "2") {
        return false;
    }
    // Kitty modifiers are one-based; ignore CapsLock and NumLock, not Shift/Alt/Super.
    matches!(modifiers.parse::<u32>(), Ok(value) if value > 0 && ((value - 1) & !192) == 4)
}

/// Owns a duplicate of stdin so restoration uses the same terminal even if stdin changes.
pub struct TerminalGuard {
    fd: OwnedFd,
    original: libc::termios,
    output: Option<OwnedFd>,
}

impl TerminalGuard {
    pub fn enter() -> Result<Self> {
        let mut terminal = Self::enter_fd(libc::STDIN_FILENO)?;
        ensure!(
            unsafe { libc::isatty(libc::STDOUT_FILENO) } == 1,
            "attachment output must be a terminal"
        );
        let output = unsafe { libc::fcntl(libc::STDOUT_FILENO, libc::F_DUPFD_CLOEXEC, 0) };
        ensure!(
            output >= 0,
            "duplicate terminal output: {}",
            std::io::Error::last_os_error()
        );
        terminal.output = Some(unsafe { OwnedFd::from_raw_fd(output) });
        Ok(terminal)
    }

    fn enter_fd(fd: RawFd) -> Result<Self> {
        // SAFETY: fcntl duplicates an existing descriptor; the result is owned exactly once.
        let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
        ensure!(
            duplicate >= 0,
            "duplicate terminal fd: {}",
            std::io::Error::last_os_error()
        );
        let fd = unsafe { OwnedFd::from_raw_fd(duplicate) };
        let mut original = std::mem::MaybeUninit::uninit();
        // SAFETY: tcgetattr initializes the supplied termios on success.
        ensure!(
            unsafe { libc::tcgetattr(fd.as_raw_fd(), original.as_mut_ptr()) } == 0,
            "read terminal attributes: {}",
            std::io::Error::last_os_error()
        );
        let original = unsafe { original.assume_init() };
        let mut raw = original;
        // SAFETY: raw is a valid initialized termios for this terminal.
        unsafe { libc::cfmakeraw(&mut raw) };
        ensure!(
            unsafe { libc::tcsetattr(fd.as_raw_fd(), libc::TCSANOW, &raw) } == 0,
            "enable raw terminal mode: {}",
            std::io::Error::last_os_error()
        );
        Ok(Self {
            fd,
            original,
            output: None,
        })
    }

    pub fn size(&self) -> Result<Size> {
        let mut size = std::mem::MaybeUninit::<libc::winsize>::uninit();
        // SAFETY: ioctl writes a winsize on success; fd remains owned by the guard.
        ensure!(
            unsafe { libc::ioctl(self.fd.as_raw_fd(), libc::TIOCGWINSZ, size.as_mut_ptr()) } == 0,
            "read terminal size: {}",
            std::io::Error::last_os_error()
        );
        let size = unsafe { size.assume_init() };
        Size {
            rows: size.ws_row,
            cols: size.ws_col,
        }
        .validate()
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        if let Some(output) = &self.output {
            let reset = output.try_clone().map(std::fs::File::from).and_then(|mut file| {
                file.write_all(b"\x1b[?9l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[>4;0m\x1b[?1l\x1b>\x1b[0 q\x1b[0m\x1b[r\x1b[?6l\x1b[?7h\x1b[?25h\r\n")
            });
            if let Err(error) = reset {
                eprintln!("remote-agent-rs: restore terminal modes: {error}");
            }
        }
        // SAFETY: original was captured from this still-open terminal descriptor.
        if unsafe { libc::tcsetattr(self.fd.as_raw_fd(), libc::TCSANOW, &self.original) } != 0 {
            eprintln!(
                "remote-agent: restore terminal attributes: {}",
                std::io::Error::last_os_error()
            );
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ClipboardBackend {
    Wayland,
    X11,
    MacOs,
}

/// An explicit environment snapshot makes selection testable without changing process state.
pub fn select_clipboard_backend(
    platform: &str,
    wayland: Option<&OsStr>,
    display: Option<&OsStr>,
) -> Result<ClipboardBackend> {
    match platform {
        "macos" => Ok(ClipboardBackend::MacOs),
        "linux" if wayland.is_some_and(|s| !s.is_empty()) => Ok(ClipboardBackend::Wayland),
        "linux" if display.is_some_and(|s| !s.is_empty()) => Ok(ClipboardBackend::X11),
        "linux" => bail!("clipboard needs WAYLAND_DISPLAY or DISPLAY"),
        _ => bail!("clipboard is unsupported on {platform}"),
    }
}

pub fn read_clipboard_png() -> Result<Vec<u8>> {
    let wayland = std::env::var_os("WAYLAND_DISPLAY");
    let display = std::env::var_os("DISPLAY");
    let backend =
        select_clipboard_backend(std::env::consts::OS, wayland.as_deref(), display.as_deref())?;
    let program = match backend {
        ClipboardBackend::Wayland => "wl-paste",
        ClipboardBackend::X11 => "xclip",
        ClipboardBackend::MacOs => "osascript",
    };
    read_clipboard_png_with(backend, Path::new(program))
}

const MACOS_CLIPBOARD_SCRIPT: &str = r#"
ObjC.import('AppKit');
function run() {
    const image = $.NSImage.alloc.initWithPasteboard($.NSPasteboard.generalPasteboard);
    if (image.isNil()) throw new Error('clipboard has no image');
    const bitmap = $.NSBitmapImageRep.imageRepWithData(image.TIFFRepresentation);
    if (bitmap.isNil()) throw new Error('clipboard image cannot be decoded');
    const png = bitmap.representationUsingTypeProperties(
        $.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary);
    if (png.isNil()) throw new Error('clipboard image cannot be converted to PNG');
    return ObjC.unwrap(png.base64EncodedStringWithOptions(0));
}
"#;

/// Runs one selected provider, never a fallback or cache. `program` can be a fixture executable.
pub fn read_clipboard_png_with(backend: ClipboardBackend, program: &Path) -> Result<Vec<u8>> {
    let mut command = Command::new(program);
    let max_output = match backend {
        ClipboardBackend::Wayland => {
            command.args(["--no-newline", "--type", "image/png"]);
            MAX_IMAGE_BYTES
        }
        ClipboardBackend::X11 => {
            command.args(["-selection", "clipboard", "-t", "image/png", "-o"]);
            MAX_IMAGE_BYTES
        }
        ClipboardBackend::MacOs => {
            command.args(["-l", "JavaScript", "-e", MACOS_CLIPBOARD_SCRIPT]);
            MAX_IMAGE_BYTES.div_ceil(3) * 4 + 2
        }
    };
    let output = read_command_bounded(command, max_output)?;
    let png = if backend == ClipboardBackend::MacOs {
        STANDARD
            .decode(output.trim_ascii())
            .context("macOS clipboard returned invalid base64")?
    } else {
        output
    };
    ensure!(!png.is_empty(), "clipboard image is empty");
    ensure!(
        png.len() <= MAX_IMAGE_BYTES,
        "clipboard PNG exceeds {MAX_IMAGE_BYTES}-byte limit"
    );
    ensure!(
        png.starts_with(b"\x89PNG\r\n\x1a\n"),
        "clipboard content does not have a PNG signature"
    );
    Ok(png)
}

fn read_command_bounded(mut command: Command, limit: usize) -> Result<Vec<u8>> {
    let description = format!("{:?}", command.get_program());
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("start clipboard provider {description}"))?;
    let stderr = child.stderr.take().expect("piped stderr");
    // Drain stderr concurrently to avoid blocking a provider before stdout is available.
    let error_reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take(64 * 1024)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let mut output = Vec::new();
    let read = child
        .stdout
        .take()
        .expect("piped stdout")
        .take(limit as u64 + 1)
        .read_to_end(&mut output);
    if (read.is_err() || output.len() > limit)
        && let Err(error) = child.kill()
    {
        eprintln!("remote-agent: stop clipboard provider {description}: {error}");
    }
    let status = child
        .wait()
        .with_context(|| format!("wait for clipboard provider {description}"));
    let stderr = error_reader
        .join()
        .map_err(|_| anyhow::anyhow!("clipboard stderr reader panicked"))?
        .context("read clipboard provider stderr")?;
    read.with_context(|| format!("read clipboard provider {description}"))?;
    ensure!(
        output.len() <= limit,
        "clipboard provider {description} output exceeds {limit}-byte limit"
    );
    let status = status?;
    ensure!(
        status.success(),
        "clipboard provider {description} failed ({status}): {}",
        String::from_utf8_lossy(&stderr).trim()
    );
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::{Engine, engine::general_purpose::STANDARD};
    use std::{
        fs,
        os::fd::{AsRawFd, FromRawFd, OwnedFd},
        os::unix::fs::PermissionsExt,
    };

    fn normalize(actions: Vec<InputAction>) -> Vec<InputAction> {
        let mut output = Vec::new();
        for action in actions {
            match action {
                InputAction::Bytes(bytes) if bytes.is_empty() => {}
                InputAction::Bytes(bytes) => match output.last_mut() {
                    Some(InputAction::Bytes(previous)) => previous.extend(bytes),
                    _ => output.push(InputAction::Bytes(bytes)),
                },
                other => output.push(other),
            }
        }
        output
    }

    fn decode(chunks: &[&[u8]]) -> Vec<InputAction> {
        let mut decoder = InputDecoder::new();
        let mut output = Vec::new();
        for chunk in chunks {
            output.extend(decoder.feed(chunk));
        }
        output.extend(decoder.finish());
        normalize(output)
    }

    #[test]
    fn legacy_shortcuts_preserve_surrounding_bytes() {
        assert_eq!(
            decode(&[b"ab\x16cd\x1cef"]),
            vec![
                InputAction::Bytes(b"ab".to_vec()),
                InputAction::PasteImage,
                InputAction::Bytes(b"cd".to_vec()),
                InputAction::Detach,
                InputAction::Bytes(b"ef".to_vec()),
            ]
        );
    }

    #[test]
    fn tokens_work_at_every_fragment_boundary() {
        let input = b"a\x1b[118;5u\x1b[200~\x16\x1c\x1b[118;5u\x1b[201~\x16z";
        let expected = vec![
            InputAction::Bytes(b"a".to_vec()),
            InputAction::PasteImage,
            InputAction::Bytes(b"\x1b[200~\x16\x1c\x1b[118;5u\x1b[201~".to_vec()),
            InputAction::PasteImage,
            InputAction::Bytes(b"z".to_vec()),
        ];
        for split in 0..=input.len() {
            assert_eq!(
                decode(&[&input[..split], &input[split..]]),
                expected,
                "split {split}"
            );
        }
        let chunks: Vec<_> = input.chunks(1).collect();
        assert_eq!(decode(&chunks), expected);
    }

    #[test]
    fn xterm_image_paste_and_enhanced_detach_shortcuts_work_at_every_split() {
        for sequence in [b"\x1b[27;5;118~".as_slice(), b"\x1b[27;5;86~"] {
            for split in 0..=sequence.len() {
                let mut decoder = InputDecoder::new();
                let mut actions = decoder.feed(&sequence[..split]);
                actions.extend(decoder.feed(&sequence[split..]));
                assert_eq!(actions, vec![InputAction::PasteImage]);
            }
        }
        for sequence in [b"\x1b[27;5;92~".as_slice(), b"\x1b[92;5u"] {
            let mut decoder = InputDecoder::new();
            assert_eq!(decoder.feed(sequence), vec![InputAction::Detach]);
        }
    }

    #[test]
    fn kitty_ctrl_v_press_repeat_and_lock_modifiers() {
        for sequence in [
            b"\x1b[118;5u".as_slice(),
            b"\x1b[86;5u",
            b"\x1b[118;5:1u",
            b"\x1b[118;5:2u",
            b"\x1b[118:86:118;5u",
            b"\x1b[118;69u",
            b"\x1b[118;133u",
        ] {
            assert_eq!(
                decode(&[sequence]),
                vec![InputAction::PasteImage],
                "{sequence:?}"
            );
        }
    }

    #[test]
    fn arbitrary_csi_utf8_escape_and_partial_tokens_are_literal() {
        for bytes in [
            b"\x1b".as_slice(),
            b"\x1b[",
            b"\x1b[20",
            b"\x1bx",
            b"\x1b[A",
            b"\x1b[118;1u",
            b"\x1b[118;7u",
            b"\x1b[118;5:3u",
            b"\x1b[118;5;99u",
            b"\x1b[999999999999999999999999999999;5u",
            "é漢🦀".as_bytes(),
        ] {
            let chunks: Vec<_> = bytes.chunks(1).collect();
            assert_eq!(decode(&chunks), vec![InputAction::Bytes(bytes.to_vec())]);
        }
        let mut decoder = InputDecoder::default();
        decoder.feed(b"\x1b[");
        assert_eq!(
            decoder.finish(),
            vec![InputAction::Bytes(b"\x1b[".to_vec())]
        );
        assert!(decoder.finish().is_empty());
    }

    #[test]
    fn huge_bracketed_paste_streams_without_changing_controls() {
        let mut decoder = InputDecoder::new();
        assert_eq!(
            decode(&[b"\x1b[200~\x1b[201~"]),
            vec![InputAction::Bytes(b"\x1b[200~\x1b[201~".to_vec())]
        );
        decoder.feed(b"\x1b[200~");
        let block = [b'x', 0x16, 0x1c, 0xff].repeat(4096);
        for _ in 0..1025 {
            assert_eq!(
                normalize(decoder.feed(&block)),
                vec![InputAction::Bytes(block.clone())]
            );
        }
        assert_eq!(
            normalize(decoder.feed(b"\x1b[201~\x16")),
            vec![
                InputAction::Bytes(b"\x1b[201~".to_vec()),
                InputAction::PasteImage
            ]
        );
    }

    #[test]
    fn explicit_clipboard_platform_selection() {
        let env = Some(OsStr::new(":1"));
        assert_eq!(
            select_clipboard_backend("linux", env, env).unwrap(),
            ClipboardBackend::Wayland
        );
        assert_eq!(
            select_clipboard_backend("linux", None, env).unwrap(),
            ClipboardBackend::X11
        );
        assert_eq!(
            select_clipboard_backend("linux", Some(OsStr::new("")), env).unwrap(),
            ClipboardBackend::X11
        );
        assert_eq!(
            select_clipboard_backend("macos", None, None).unwrap(),
            ClipboardBackend::MacOs
        );
        assert!(select_clipboard_backend("linux", None, None).is_err());
        assert!(select_clipboard_backend("windows", env, env).is_err());
    }

    // A real child process emits fixture bytes; no shared environment mutation or shell.
    fn fake_program(body: &str) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("clipboard");
        fs::write(&path, format!("#!/usr/bin/python3\nimport sys\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        (dir, path)
    }

    const PNG: &[u8] = b"\x89PNG\r\n\x1a\nfixture";

    #[test]
    fn clipboard_commands_return_current_png_with_expected_arguments() {
        for (backend, args) in [
            (
                ClipboardBackend::Wayland,
                "['--no-newline', '--type', 'image/png']",
            ),
            (
                ClipboardBackend::X11,
                "['-selection', 'clipboard', '-t', 'image/png', '-o']",
            ),
        ] {
            let (_dir, program) = fake_program(
                &format!("assert sys.argv[1:] == {args}\nsys.stdout.buffer.write({PNG:?})")
                    .replace(&format!("{PNG:?}"), "b'\\x89PNG\\r\\n\\x1a\\nfixture'"),
            );
            assert_eq!(read_clipboard_png_with(backend, &program).unwrap(), PNG);
        }
        let encoded = STANDARD.encode(PNG);
        let (_dir, program) = fake_program(&format!(
            "assert sys.argv[1:4] == ['-l', 'JavaScript', '-e']\nprint('{encoded}')"
        ));
        assert_eq!(
            read_clipboard_png_with(ClipboardBackend::MacOs, &program).unwrap(),
            PNG
        );
    }

    #[test]
    fn clipboard_rejects_empty_invalid_oversize_and_command_errors() {
        for (body, expected) in [
            ("pass".to_string(), "empty"),
            ("sys.stdout.buffer.write(b'not PNG')".to_string(), "PNG"),
            (
                format!("sys.stdout.buffer.write(b'x' * {})", MAX_IMAGE_BYTES + 1),
                "limit",
            ),
            (
                "sys.stderr.write('clipboard unavailable'); sys.exit(2)".to_string(),
                "clipboard unavailable",
            ),
        ] {
            let (_dir, program) = fake_program(&body);
            let error = read_clipboard_png_with(ClipboardBackend::Wayland, &program).unwrap_err();
            assert!(format!("{error:#}").contains(expected), "{error:#}");
        }
        let (_dir, program) = fake_program("print('not-base64!!')");
        assert!(
            format!(
                "{:#}",
                read_clipboard_png_with(ClipboardBackend::MacOs, &program).unwrap_err()
            )
            .contains("base64")
        );
        assert!(
            read_clipboard_png_with(ClipboardBackend::Wayland, Path::new("/does/not/exist"))
                .is_err()
        );
    }

    fn pty() -> (OwnedFd, OwnedFd) {
        let mut master = -1;
        let mut slave = -1;
        assert_eq!(
            unsafe {
                libc::openpty(
                    &mut master,
                    &mut slave,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                )
            },
            0
        );
        unsafe { (OwnedFd::from_raw_fd(master), OwnedFd::from_raw_fd(slave)) }
    }

    fn termios(fd: &OwnedFd) -> libc::termios {
        let mut value = std::mem::MaybeUninit::uninit();
        assert_eq!(
            unsafe { libc::tcgetattr(fd.as_raw_fd(), value.as_mut_ptr()) },
            0
        );
        unsafe { value.assume_init() }
    }

    fn pty_readable(fd: &OwnedFd, timeout_ms: i32) -> bool {
        let mut poll = libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut poll, 1, timeout_ms) };
        assert!(result >= 0, "poll PTY: {}", std::io::Error::last_os_error());
        if result == 0 {
            return false;
        }
        assert_eq!(poll.revents, libc::POLLIN);
        true
    }

    fn write_pty(fd: &OwnedFd, bytes: &[u8]) {
        assert_eq!(
            unsafe { libc::write(fd.as_raw_fd(), bytes.as_ptr().cast(), bytes.len()) },
            bytes.len() as isize,
            "write PTY: {}",
            std::io::Error::last_os_error()
        );
    }

    fn assert_pty_bytes(fd: &OwnedFd, expected: &[u8]) {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        let mut received = Vec::new();
        while received.len() < expected.len() {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            assert!(
                pty_readable(fd, remaining.as_millis() as i32),
                "PTY read timed out: {received:?}, expected {expected:?}"
            );
            let mut buffer = vec![0; expected.len() - received.len()];
            let count =
                unsafe { libc::read(fd.as_raw_fd(), buffer.as_mut_ptr().cast(), buffer.len()) };
            assert!(count > 0, "read PTY: {}", std::io::Error::last_os_error());
            received.extend_from_slice(&buffer[..count as usize]);
        }
        assert_eq!(received, expected);
    }

    #[test]
    fn terminal_guard_uses_raw_mode_reads_size_and_restores_on_drop() {
        let (master, slave) = pty();
        let mut configured = termios(&slave);
        configured.c_lflag |= libc::ICANON | libc::ECHO;
        configured.c_oflag |= libc::OPOST | libc::ONLCR;
        assert_eq!(
            unsafe { libc::tcsetattr(slave.as_raw_fd(), libc::TCSANOW, &configured) },
            0
        );
        let before = termios(&slave);
        let size = libc::winsize {
            ws_row: 31,
            ws_col: 97,
            ws_xpixel: 0,
            ws_ypixel: 0,
        };
        assert_eq!(
            unsafe { libc::ioctl(slave.as_raw_fd(), libc::TIOCSWINSZ, &size) },
            0
        );
        let guard = TerminalGuard::enter_fd(slave.as_raw_fd()).unwrap();
        let raw = termios(&slave);
        assert_eq!(
            raw.c_lflag & (libc::ICANON | libc::ECHO | libc::ISIG | libc::IEXTEN),
            0
        );
        assert_eq!(raw.c_iflag & (libc::IXON | libc::ICRNL), 0);
        assert_eq!(raw.c_oflag & libc::OPOST, 0);
        assert_eq!(guard.size().unwrap(), Size { rows: 31, cols: 97 });
        write_pty(&master, b"raw");
        assert_pty_bytes(&slave, b"raw");
        assert!(!pty_readable(&master, 50), "raw input must not echo");
        drop(guard);
        let after = termios(&slave);
        // Apple XNU bsd/sys/termios.h defines PENDIN as pending-input retype
        // state, not a configured mode: https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/termios.h
        #[cfg(target_os = "macos")]
        let state_flags = libc::PENDIN;
        #[cfg(not(target_os = "macos"))]
        let state_flags = 0;
        assert_eq!(
            (
                after.c_iflag,
                after.c_oflag,
                after.c_cflag,
                after.c_lflag & !state_flags,
                after.c_cc
            ),
            (
                before.c_iflag,
                before.c_oflag,
                before.c_cflag,
                before.c_lflag & !state_flags,
                before.c_cc
            )
        );
        assert_eq!(unsafe { libc::cfgetispeed(&after) }, unsafe {
            libc::cfgetispeed(&before)
        });
        assert_eq!(unsafe { libc::cfgetospeed(&after) }, unsafe {
            libc::cfgetospeed(&before)
        });
        write_pty(&master, b"restored");
        assert_pty_bytes(&master, b"restored");
        assert!(
            !pty_readable(&slave, 50),
            "canonical input must wait for a newline"
        );
        write_pty(&master, b"\n");
        assert_pty_bytes(&slave, b"restored\n");
        assert_pty_bytes(&master, b"\r\n");
        assert!(TerminalGuard::enter_fd(-1).is_err());
    }
}

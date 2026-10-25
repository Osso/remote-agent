//! Native scrollback transport. Snapshots describe the visible display, not a
//! serialization of vt100's hidden parser state (saved cursors, margins, or
//! partially received sequences). The authoritative parser stays on the server.

use crate::protocol::{DisplayUpdate, Size};
use anyhow::{Result, ensure};
use std::collections::VecDeque;
use std::num::NonZeroU16;

// state_formatted() emits mouse/style changes relative to their defaults; it
// does not clear an already enabled mouse mode on an existing client.
const MODE_BASELINE: &[u8] =
    b"\x1b[?9l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[0 q";

fn display_bytes(screen: &vt100::Screen) -> Vec<u8> {
    [MODE_BASELINE, screen.state_formatted().as_bytes()].concat()
}

fn dimensions(size: Size) -> (NonZeroU16, NonZeroU16) {
    (
        size.rows.try_into().expect("positive rows"),
        size.cols.try_into().expect("positive columns"),
    )
}

#[derive(Default)]
struct Capture {
    rows: Vec<Vec<u8>>,
    control: Vec<u8>,
    title: Vec<u8>,
    responses: Vec<u8>,
    failure: Option<String>,
}

impl Capture {
    fn osc(&mut self, op: &[u8], value: &[u8]) {
        self.control.extend_from_slice(b"\x1b]");
        self.control.extend_from_slice(op);
        self.control.push(b';');
        self.control.extend_from_slice(value);
        self.control.extend_from_slice(b"\x1b\\");
    }
}

impl vt100::Callbacks for Capture {
    fn on_scroll(&mut self, row: vt100::capture::RowContents<'_>, alternate: bool) {
        if alternate {
            return;
        }
        let mut formatted = String::new();
        row.write_formatted_basic(&mut formatted, &mut Default::default())
            .expect("writing a captured row into String cannot fail");
        self.rows.push(formatted.into_bytes());
    }

    fn audible_bell(&mut self, _: &mut vt100::Screen) {
        self.control.push(7);
    }

    fn set_window_title(&mut self, _: &mut vt100::Screen, title: &[u8]) {
        self.title = title.to_vec();
        self.osc(b"2", title);
    }

    fn set_window_icon_name(&mut self, _: &mut vt100::Screen, title: &[u8]) {
        self.osc(b"1", title);
    }

    fn copy_to_clipboard(&mut self, _: &mut vt100::Screen, ty: &[u8], data: &[u8]) {
        let mut value = ty.to_vec();
        value.push(b';');
        value.extend_from_slice(data);
        self.osc(b"52", &value);
    }

    fn paste_from_clipboard(&mut self, _: &mut vt100::Screen, _: &[u8]) {
        self.failure = Some("clipboard read queries are unsupported".into());
    }

    fn resize(&mut self, _: &mut vt100::Screen, request: (u16, u16)) {
        self.failure = Some(format!(
            "application-requested terminal resize is unsupported: {request:?}"
        ));
    }

    fn unhandled_csi(
        &mut self,
        screen: &mut vt100::Screen,
        i1: Option<u8>,
        i2: Option<u8>,
        params: &[&[u16]],
        c: char,
    ) {
        let parameter = params.first().and_then(|p| p.first()).copied().unwrap_or(0);
        let response = match (i1, i2, c, parameter) {
            (Some(b'?'), None, 'u', 0) => Some("\x1b[?0u".to_string()),
            (None, None, 'n', 5) => Some("\x1b[0n".to_string()),
            (None, None, 'n', 6) => {
                let (row, col) = screen.cursor_position();
                Some(format!("\x1b[{};{}R", row + 1, col + 1))
            }
            (None, None, 'c', 0) => Some("\x1b[?1;2c".to_string()),
            (Some(b'>'), None, 'c', 0) => Some("\x1b[>0;0;0c".to_string()),
            // Synchronized painting does not change the display model.
            (Some(b'?'), None, 'h' | 'l', 2026) if params.len() == 1 => None,
            _ => {
                self.failure = Some(format!("unsupported CSI: {i1:?} {i2:?} {params:?} {c}"));
                None
            }
        };
        if let Some(response) = response {
            self.responses.extend_from_slice(response.as_bytes());
        }
    }

    fn unhandled_escape(&mut self, _: &mut vt100::Screen, a: Option<u8>, b: Option<u8>, c: u8) {
        self.failure = Some(format!("unsupported terminal escape: {a:?} {b:?} {c}"));
    }
    fn unhandled_osc(&mut self, _: &mut vt100::Screen, params: &[&[u8]]) {
        self.failure = Some(format!("unsupported terminal OSC: {params:?}"));
    }
    fn unhandled_char(&mut self, _: &mut vt100::Screen, c: char) {
        self.failure = Some(format!("unsupported or invalid terminal character: {c:?}"));
    }
    fn unhandled_control(&mut self, _: &mut vt100::Screen, b: u8) {
        // NUL and DEL are padding, not terminal state.
        if !matches!(b, 0 | 127) {
            self.failure = Some(format!("unsupported terminal control: {b}"));
        }
    }
}

/// A persistent vt100 parser with byte-bounded formatted history and journal.
/// `history_bytes` independently caps retained rows and replay updates. A live
/// feed still returns EVERY row, even when that one update exceeds the budget.
///
/// Unsupported callback operations poison this state and return an error.
/// Snapshots remain at the last successful display. This is not a terminal
/// sanitizer: vt100 itself ignores some unsupported modes and escape strings.
pub struct TerminalState {
    parser: vt100::Parser<Capture>,
    size: Size,
    sequence: u64,
    budget: usize,
    history: VecDeque<Vec<u8>>,
    history_len: usize,
    journal: VecDeque<DisplayUpdate>,
    journal_len: usize,
    screen: Vec<u8>,
}

impl TerminalState {
    pub fn new(size: Size, history_bytes: usize) -> Result<Self> {
        size.validate()?;
        let (rows, cols) = dimensions(size);
        let parser = vt100::Parser::new_with_callbacks(rows, cols, 0, Capture::default());
        let screen = display_bytes(parser.screen());
        Ok(Self {
            parser,
            size,
            sequence: 0,
            budget: history_bytes,
            history: VecDeque::new(),
            history_len: 0,
            journal: VecDeque::new(),
            journal_len: 0,
            screen,
        })
    }

    pub fn feed(&mut self, data: &[u8]) -> Result<DisplayUpdate> {
        self.ensure_supported()?;
        self.parser.process(data);
        self.ensure_supported()?;
        Ok(self.publish())
    }

    pub fn drain_responses(&mut self) -> Vec<u8> {
        std::mem::take(&mut self.parser.callbacks_mut().responses)
    }

    fn ensure_supported(&self) -> Result<()> {
        if let Some(failure) = &self.parser.callbacks().failure {
            anyhow::bail!("{failure}");
        }
        ensure!(
            !self.parser.screen().alternate_screen(),
            "alternate-screen applications are unsupported by the native main-screen renderer"
        );
        Ok(())
    }

    pub fn resize(&mut self, size: Size) -> Result<DisplayUpdate> {
        size.validate()?;
        self.ensure_supported()?;
        let (rows, cols) = dimensions(size);
        self.parser.set_size(rows, cols);
        self.size = size;
        Ok(self.publish())
    }

    pub fn snapshot(&self, after: Option<u64>) -> DisplayUpdate {
        if let Some(updates) = after.and_then(|sequence| self.updates_since(sequence)) {
            let mut snapshot = self.display();
            snapshot.scrolled = updates
                .iter()
                .flat_map(|u| u.scrolled.iter().cloned())
                .collect();
            snapshot.control = updates
                .iter()
                .flat_map(|u| u.control.iter().copied())
                .collect();
            return snapshot;
        }
        let mut snapshot = self.display();
        snapshot.scrolled = self.history.iter().cloned().collect();
        snapshot.reset = true;
        snapshot.expired = after.is_some();
        let title = &self.parser.callbacks().title;
        if !title.is_empty() {
            snapshot.control = [b"\x1b]2;".as_slice(), title, b"\x1b\\"].concat();
        }
        snapshot
    }

    pub fn updates_since(&self, after: u64) -> Option<Vec<DisplayUpdate>> {
        if after > self.sequence {
            return None;
        }
        if after == self.sequence {
            return Some(Vec::new());
        }
        let first = self.journal.front()?;
        if after < first.sequence - 1 {
            return None;
        }
        Some(
            self.journal
                .iter()
                .filter(|u| u.sequence > after)
                .cloned()
                .collect(),
        )
    }

    fn display(&self) -> DisplayUpdate {
        DisplayUpdate {
            sequence: self.sequence,
            size: self.size,
            scrolled: Vec::new(),
            screen: self.screen.clone(),
            control: Vec::new(),
            reset: false,
            expired: false,
        }
    }

    fn publish(&mut self) -> DisplayUpdate {
        self.sequence = self
            .sequence
            .checked_add(1)
            .expect("display sequence exhausted");
        self.screen = display_bytes(self.parser.screen());
        let mut update = self.display();
        update.scrolled = std::mem::take(&mut self.parser.callbacks_mut().rows);
        update.control = std::mem::take(&mut self.parser.callbacks_mut().control);
        for row in &update.scrolled {
            self.history_len += row.len() + 24;
            self.history.push_back(row.clone());
        }
        while self.history_len > self.budget {
            self.history_len -= self
                .history
                .pop_front()
                .expect("history bytes accounted")
                .len()
                + 24;
        }
        self.journal_len += update_bytes(&update);
        self.journal.push_back(update.clone());
        while self.journal_len > self.budget {
            self.journal_len -=
                update_bytes(&self.journal.pop_front().expect("journal bytes accounted"));
        }
        update
    }
}

fn update_bytes(update: &DisplayUpdate) -> usize {
    // Include fixed fields and per-row allocation overhead, including blanks.
    64 + update.screen.len()
        + update.control.len()
        + update
            .scrolled
            .iter()
            .map(|row| row.len() + 24)
            .sum::<usize>()
}

/// Renders the virtual screen at physical row zero without an alternate screen.
/// Physical size is validated by render(), as construction/resize are infallible.
pub struct DisplayRenderer {
    physical: Size,
    sequence: Option<u64>,
    repaint: bool,
    native_rows: usize,
    restore_rows: usize,
}

impl DisplayRenderer {
    pub fn new(physical: Size) -> Self {
        Self {
            physical,
            sequence: None,
            repaint: true,
            native_rows: 0,
            restore_rows: 0,
        }
    }

    pub fn resize_physical(&mut self, physical: Size) {
        // Native terminals can pull old scrollback into the viewport when
        // growing. Push those rows back before clearing the virtual viewport.
        let growth = usize::from(physical.rows.saturating_sub(self.physical.rows));
        self.restore_rows += growth.min(self.native_rows.saturating_sub(self.restore_rows));
        self.physical = physical;
        self.repaint = true;
    }

    pub fn render(&mut self, update: &DisplayUpdate) -> Result<Vec<u8>> {
        self.physical.validate()?;
        update.size.validate()?;
        ensure!(
            self.physical.rows >= update.size.rows && self.physical.cols >= update.size.cols,
            "physical terminal is smaller than the negotiated PTY size"
        );
        let duplicate = self
            .sequence
            .is_some_and(|sequence| update.sequence <= sequence);
        if duplicate && !update.reset && !self.repaint {
            return Ok(Vec::new());
        }
        if let Some(sequence) = self.sequence {
            if update.sequence < sequence {
                return Ok(Vec::new());
            }
        }
        let mut output = b"\x1b[?6l\x1b[r\x1b[0m\x1b[?7l".to_vec();
        let mut native_rows = self.native_rows;
        if update.reset {
            output.extend_from_slice(b"\x1b[3J\x1b[2J\x1b[H");
            native_rows = 0;
        } else {
            for _ in 0..self.restore_rows {
                self.scroll_physical(&mut output);
            }
        }
        if update.reset || !duplicate {
            for row in &update.scrolled {
                native_rows += self.push_history_row(&mut output, row)?;
            }
        }
        output.extend_from_slice(b"\x1b[0m\x1b[H\x1b[2J\x1b[?7h");
        repaint_screen(&mut output, update);
        if !duplicate || update.reset {
            output.extend_from_slice(&update.control);
        }
        self.sequence = Some(update.sequence);
        self.native_rows = native_rows;
        self.restore_rows = 0;
        self.repaint = false;
        Ok(output)
    }

    fn scroll_physical(&self, output: &mut Vec<u8>) {
        output.extend_from_slice(format!("\x1b[0m\x1b[{};1H\n", self.physical.rows).as_bytes());
    }

    fn push_history_row(&self, output: &mut Vec<u8>, row: &[u8]) -> Result<usize> {
        let parts = split_history_row(row, self.physical.cols)?;
        let count = parts.len();
        for part in parts {
            output.extend_from_slice(b"\x1b[H\x1b[0m\x1b[2K");
            output.extend_from_slice(&part);
            self.scroll_physical(output);
        }
        Ok(count)
    }
}

fn repaint_screen(output: &mut Vec<u8>, update: &DisplayUpdate) {
    let (rows, cols) = dimensions(update.size);
    let mut parser = vt100::Parser::new(rows, cols, 0);
    parser.process(&update.screen);
    let screen = parser.screen();
    // Virtual soft wraps must not depend on the larger physical width.
    for row in 0..update.size.rows {
        output.extend_from_slice(format!("\x1b[{};1H", row + 1).as_bytes());
        for col in 0..update.size.cols {
            let cell = screen.cell(row, col).expect("cell within virtual screen");
            if !cell.is_wide_continuation() {
                output.extend_from_slice(&format_cell(cell));
            }
        }
    }
    let (row, col) = screen.cursor_position();
    let col = col.min(update.size.cols - 1);
    output.extend_from_slice(format!("\x1b[{};{}H", row + 1, col + 1).as_bytes());
    output.extend_from_slice(if screen.hide_cursor() {
        b"\x1b[?25l"
    } else {
        b"\x1b[?25h"
    });
    output.extend_from_slice(MODE_BASELINE);
    output.extend_from_slice(screen.input_mode_formatted().as_bytes());
    output.extend_from_slice(screen.attributes_formatted().as_bytes());
}

fn split_history_row(row: &[u8], width: u16) -> Result<Vec<Vec<u8>>> {
    // Captured rows contain only UTF-8 text and SGR. Parse at the maximum
    // supported old width, then split by actual cell widths, not byte offsets.
    let mut parser = vt100::Parser::new(1.try_into().unwrap(), 1000.try_into().unwrap(), 0);
    parser.process(row);
    let screen = parser.screen();
    let end = (0..1000)
        .rfind(|col| {
            screen
                .cell(0, *col)
                .is_some_and(|cell| cell.has_contents() || cell.bgcolor() != vt100::Color::Default)
        })
        .map_or(0, |col| col + 1);
    let mut parts = vec![Vec::new()];
    let mut used = 0;
    for col in 0..end {
        let cell = screen.cell(0, col).expect("column within parsed row");
        if cell.is_wide_continuation() {
            continue;
        }
        let cells = if cell.is_wide() { 2 } else { 1 };
        ensure!(
            cells <= width,
            "physical terminal cannot display a wide character"
        );
        if used + cells > width {
            parts.push(Vec::new());
            used = 0;
        }
        let part = parts.last_mut().expect("at least one part");
        part.extend_from_slice(&format_cell(cell));
        used += cells;
    }
    Ok(parts)
}

fn format_cell(cell: &vt100::Cell) -> Vec<u8> {
    let mut codes = vec!["0".to_string()];
    for (enabled, code) in [
        (cell.bold(), "1"),
        (cell.dim(), "2"),
        (cell.italic(), "3"),
        (cell.underline(), "4"),
        (cell.inverse(), "7"),
    ] {
        if enabled {
            codes.push(code.to_string());
        }
    }
    for (color, prefix) in [(cell.fgcolor(), 38), (cell.bgcolor(), 48)] {
        match color {
            vt100::Color::Default => {}
            vt100::Color::Idx(index) => codes.push(format!("{prefix};5;{index}")),
            vt100::Color::Rgb(r, g, b) => codes.push(format!("{prefix};2;{r};{g};{b}")),
        }
    }
    let contents = if cell.has_contents() {
        cell.contents()
    } else {
        " "
    };
    format!("\x1b[{}m{contents}", codes.join(";")).into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn size(rows: u16, cols: u16) -> Size {
        Size { rows, cols }
    }
    fn parser(size: Size, history: usize) -> vt100::Parser {
        vt100::Parser::new(
            size.rows.try_into().unwrap(),
            size.cols.try_into().unwrap(),
            history,
        )
    }
    fn all_text(parser: &mut vt100::Parser) -> String {
        let mut lines = Vec::new();
        let count = parser.screen().scrollback_len();
        for offset in (1..=count).rev() {
            parser.screen_mut().set_scrollback(offset);
            lines.push(parser.screen().rows(0, 1000).next().unwrap());
        }
        parser.screen_mut().set_scrollback(0);
        lines.push(parser.screen().contents());
        lines.join("\n")
    }
    fn bulk() -> String {
        (0..80).map(|n| format!("marker-{n:03}\r\n")).collect()
    }
    fn assert_markers(text: &str) {
        for n in 0..80 {
            let marker = format!("marker-{n:03}");
            assert_eq!(text.matches(&marker).count(), 1, "{marker}: {text}");
        }
    }

    #[test]
    fn bulk_scrollback_survives_larger_physical_screens_and_dedup() {
        for physical in [size(12, 30), size(24, 50), size(40, 80)] {
            let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
            let update = state.feed(bulk().as_bytes()).unwrap();
            assert_eq!(update.scrolled.len(), 76);
            let mut renderer = DisplayRenderer::new(physical);
            let mut outer = parser(physical, 200);
            outer.process(&renderer.render(&update).unwrap());
            assert!(renderer.render(&update).unwrap().is_empty());
            assert_markers(&all_text(&mut outer));
            outer.process(
                &renderer
                    .render(&state.snapshot(Some(update.sequence)))
                    .unwrap(),
            );
            assert_markers(&all_text(&mut outer));
        }
    }

    #[test]
    fn fragmented_unicode_and_ansi_match_whole_feed_and_snapshot_modes() {
        let input =
            "\x1b[?2004h\x1b[?1h\x1b[?25l\x1b[31m界e\u{301}🙂\x1b[0m\r\nsecond\r\nthird\r\nlast";
        let mut whole = TerminalState::new(size(3, 20), 100_000).unwrap();
        whole.feed(input.as_bytes()).unwrap();
        let mut split = TerminalState::new(size(3, 20), 100_000).unwrap();
        for b in input.as_bytes() {
            split.feed(&[*b]).unwrap();
        }
        let a = whole.snapshot(None);
        let b = split.snapshot(None);
        assert_eq!(a.screen, b.screen);
        assert_eq!(a.scrolled, b.scrolled);
        let mut outer = parser(size(10, 30), 100);
        outer.process(&DisplayRenderer::new(size(10, 30)).render(&b).unwrap());
        assert!(outer.screen().application_cursor());
        assert!(outer.screen().bracketed_paste());
        assert!(outer.screen().hide_cursor());
        assert!(all_text(&mut outer).contains("界e\u{301}🙂"));
    }

    #[test]
    fn bounded_history_and_journal_report_expiry() {
        let mut state = TerminalState::new(size(3, 20), 150).unwrap();
        state.feed(bulk().as_bytes()).unwrap();
        let snapshot = state.snapshot(Some(0));
        assert!(snapshot.reset && snapshot.expired);
        assert!(snapshot.scrolled.iter().map(Vec::len).sum::<usize>() <= 150);
        assert!(state.updates_since(0).is_none());
        assert!(state.updates_since(snapshot.sequence).unwrap().is_empty());
        assert!(state.updates_since(snapshot.sequence + 1).is_none());
    }

    #[test]
    fn replay_and_resize_repaint_do_not_duplicate_history() {
        let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
        let first = state.feed(b"start\r\n").unwrap();
        let second = state.feed(bulk().as_bytes()).unwrap();
        assert_eq!(
            state.updates_since(first.sequence).unwrap()[0].sequence,
            second.sequence
        );
        let physical = size(24, 40);
        let mut renderer = DisplayRenderer::new(physical);
        let mut outer = parser(physical, 200);
        outer.process(&renderer.render(&state.snapshot(None)).unwrap());
        let resized = state.resize(size(7, 20)).unwrap();
        renderer.resize_physical(size(30, 50));
        outer.set_size(30.try_into().unwrap(), 50.try_into().unwrap());
        outer.process(&renderer.render(&resized).unwrap());
        outer.process(
            &renderer
                .render(&state.snapshot(Some(resized.sequence)))
                .unwrap(),
        );
        assert_markers(&all_text(&mut outer));
    }

    #[test]
    fn old_wide_rows_are_split_instead_of_truncated() {
        let mut state = TerminalState::new(size(2, 40), 100_000).unwrap();
        state
            .feed(b"abcdefghijABCDEFGHIJ0123456789\r\nx\r\ny")
            .unwrap();
        state.resize(size(2, 10)).unwrap();
        let mut renderer = DisplayRenderer::new(size(8, 10));
        let mut outer = parser(size(8, 10), 100);
        outer.process(&renderer.render(&state.snapshot(None)).unwrap());
        let text = all_text(&mut outer);
        for piece in ["abcdefghij", "ABCDEFGHIJ", "0123456789"] {
            assert_eq!(text.matches(piece).count(), 1, "{text}");
        }
    }

    #[test]
    fn soft_wrapped_virtual_rows_stay_at_physical_row_zero() {
        let mut state = TerminalState::new(size(4, 10), 1000).unwrap();
        let update = state.feed(b"ABCDEFGHIJ0123456789abcdefghij").unwrap();
        let physical = size(12, 30);
        let mut outer = parser(physical, 100);
        outer.process(&DisplayRenderer::new(physical).render(&update).unwrap());
        let rows: Vec<_> = outer.screen().rows(0, 30).collect();
        assert_eq!(rows[0], "ABCDEFGHIJ");
        assert_eq!(rows[1], "0123456789");
        assert_eq!(rows[2], "abcdefghij");
        assert!(rows[3..].iter().all(String::is_empty));
    }

    #[test]
    fn disabling_mouse_and_resetting_cursor_style_restores_supported_modes() {
        let physical = size(10, 30);
        let mut outer = parser(physical, 100);
        let mut renderer = DisplayRenderer::new(physical);
        let mut state = TerminalState::new(size(5, 20), 10_000).unwrap();
        let enabled = state.feed(b"\x1b[?1003h\x1b[?1006h\x1b[5 q").unwrap();
        outer.process(&renderer.render(&enabled).unwrap());
        assert_eq!(
            outer.screen().mouse_protocol_mode(),
            vt100::MouseProtocolMode::AnyMotion
        );
        let disabled = state.feed(b"\x1b[?1003l\x1b[?1006l\x1b[0 q").unwrap();
        outer.process(&renderer.render(&disabled).unwrap());
        assert_eq!(
            outer.screen().mouse_protocol_mode(),
            vt100::MouseProtocolMode::None
        );
        assert_eq!(
            outer.screen().mouse_protocol_encoding(),
            vt100::MouseProtocolEncoding::Default
        );
        assert_eq!(outer.screen().cursor_style(), vt100::CursorStyle::Default);
    }

    #[test]
    fn streaming_each_byte_preserves_native_history_and_style() {
        let physical = size(12, 30);
        let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
        let mut renderer = DisplayRenderer::new(physical);
        let mut outer = parser(physical, 200);
        for byte in format!("\x1b[31m{}", bulk()).bytes() {
            outer.process(&renderer.render(&state.feed(&[byte]).unwrap()).unwrap());
        }
        let history_rows = outer.screen().scrollback_len();
        outer.screen_mut().set_scrollback(history_rows);
        assert_eq!(
            outer.screen().cell(0, 0).unwrap().fgcolor(),
            vt100::Color::Idx(1)
        );
        assert_markers(&all_text(&mut outer));
    }

    #[test]
    fn queries_are_answered_once_using_virtual_coordinates() {
        let mut state = TerminalState::new(size(5, 20), 1000).unwrap();
        let update = state
            .feed(b"\x1b[3;7H\x1b[6n\x1b[?u\x1b[c\x1b[>c\x1b[5n")
            .unwrap();
        assert!(update.control.is_empty());
        assert_eq!(
            state.drain_responses(),
            b"\x1b[3;7R\x1b[?0u\x1b[?1;2c\x1b[>0;0;0c\x1b[0n"
        );
        assert!(state.drain_responses().is_empty());
    }

    #[test]
    fn unsupported_operations_fail_without_publishing_partial_display() {
        let mut state = TerminalState::new(size(5, 20), 1000).unwrap();
        let before = state.feed(b"valid").unwrap();
        assert!(state.feed(b"partial\x1b[999z").is_err());
        let snapshot = state.snapshot(None);
        assert_eq!(snapshot.screen, before.screen);
        assert_eq!(snapshot.sequence, before.sequence);
        assert!(state.feed(b"more").is_err());
    }

    #[test]
    fn invalid_sizes_and_undersized_client_fail_explicitly() {
        assert!(TerminalState::new(size(0, 20), 100).is_err());
        let mut state = TerminalState::new(size(5, 20), 100).unwrap();
        assert!(
            DisplayRenderer::new(size(4, 20))
                .render(&state.feed(b"hello").unwrap())
                .is_err()
        );
        assert!(state.resize(size(0, 20)).is_err());
    }
}

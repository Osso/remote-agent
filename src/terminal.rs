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
    cwd: Vec<u8>,
    keyboard: Vec<u8>,
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
            // Pi pushes Kitty flags before querying. The virtual endpoint reports
            // no Kitty enhancement; only xterm modifyOtherKeys is forwarded.
            (Some(b'>') | Some(b'<'), None, 'u', _) if params.len() <= 1 => None,
            (Some(b'>'), None, 'm', 4) if params.len() == 2 => {
                let level = params[1].first().copied().unwrap_or(0);
                if level <= 2 {
                    self.keyboard = format!("\x1b[>4;{level}m").into_bytes();
                    self.control.extend_from_slice(&self.keyboard);
                } else {
                    self.failure = Some(format!("unsupported modifyOtherKeys level: {level}"));
                }
                None
            }
            (None, None, 'n', 5) => Some("\x1b[0n".to_string()),
            (None, None, 'n', 6) => {
                let (row, col) = screen.cursor_position();
                Some(format!("\x1b[{};{}R", row + 1, col + 1))
            }
            (None, None, 'c', 0) => Some("\x1b[?1;2c".to_string()),
            (Some(b'>'), None, 'c', 0) => Some("\x1b[>0;0;0c".to_string()),
            // This endpoint does not publish color notifications or know the
            // native appearance. Pi handles unanswered theme queries by timeout.
            (Some(b'?'), None, 'h' | 'l', 2031) if params.len() == 1 => None,
            (Some(b'?'), None, 'n', 996) if params.len() == 1 => None,
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
        if a.is_none() && b.is_none() && c == b'\\' {
            return;
        }
        self.failure = Some(format!("unsupported terminal escape: {a:?} {b:?} {c}"));
    }
    fn unhandled_osc(&mut self, _: &mut vt100::Screen, params: &[&[u8]]) {
        match params {
            // The virtual endpoint has no native background color to report.
            [b"11", b"?"] => {}
            [b"7", cwd] => {
                self.cwd = cwd.to_vec();
                self.osc(b"7", cwd);
            }
            // Pi appends a hyperlink close to every row, including plain text.
            [b"8", b"", b""] => {}
            // These advisory shell zones carry no cell data. Reframed clients
            // must not receive markers at an unrelated physical cursor position.
            [b"133", b"A" | b"B" | b"C"] => {}
            [b"9", b"4", b"3"] => self.osc(b"9", b"4;3"),
            [b"9", b"4", b"0", b""] => self.osc(b"9", b"4;0;"),
            _ => self.failure = Some(format!("unsupported terminal OSC: {params:?}")),
        }
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

#[derive(Default)]
struct ScrollbackClear {
    detected: bool,
}

impl vte::Perform for ScrollbackClear {
    fn csi_dispatch(
        &mut self,
        params: &vte::Params,
        intermediates: &[u8],
        ignore: bool,
        action: char,
    ) {
        self.detected = !ignore
            && intermediates.is_empty()
            && action == 'J'
            && params.iter().next() == Some([3].as_slice());
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
    sequence_parser: vte::Parser,
    reset: bool,
    size: Size,
    sequence: u64,
    budget: usize,
    history: VecDeque<Vec<u8>>,
    history_len: usize,
    history_evicted: bool,
    journal: VecDeque<DisplayUpdate>,
    journal_len: usize,
    /// The display at `sequence`; live updates are diffs from its predecessor.
    published: vt100::Screen,
}

impl TerminalState {
    pub fn new(size: Size, history_bytes: usize) -> Result<Self> {
        size.validate()?;
        let (rows, cols) = dimensions(size);
        let parser = vt100::Parser::new_with_callbacks(rows, cols, 0, Capture::default());
        let published = parser.screen().clone();
        Ok(Self {
            parser,
            sequence_parser: vte::Parser::new(),
            reset: false,
            size,
            sequence: 0,
            budget: history_bytes,
            history: VecDeque::new(),
            history_len: 0,
            history_evicted: false,
            journal: VecDeque::new(),
            journal_len: 0,
            published,
        })
    }

    pub fn feed(&mut self, data: &[u8]) -> Result<DisplayUpdate> {
        self.ensure_supported()?;
        let mut clear = ScrollbackClear::default();
        for byte in data {
            let byte = std::slice::from_ref(byte);
            self.parser.process(byte);
            self.sequence_parser.advance(&mut clear, byte);
            if std::mem::take(&mut clear.detected) {
                self.history.clear();
                self.history_len = 0;
                self.history_evicted = false;
                self.journal.clear();
                self.journal_len = 0;
                self.parser.callbacks_mut().rows.clear();
                self.reset = true;
            }
        }
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
            snapshot.reset = updates.iter().any(|u| u.reset);
            snapshot.expired = updates.iter().any(|u| u.expired);
            let rows_after_clear = updates.iter().rposition(|u| u.reset).unwrap_or(0);
            snapshot.scrolled = updates[rows_after_clear..]
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
        snapshot.expired = after.is_some() || self.history_evicted;
        let title = &self.parser.callbacks().title;
        if !title.is_empty() {
            snapshot.control = [b"\x1b]2;".as_slice(), title, b"\x1b\\"].concat();
        }
        let cwd = &self.parser.callbacks().cwd;
        if !cwd.is_empty() {
            snapshot
                .control
                .extend_from_slice(&[b"\x1b]7;".as_slice(), cwd, b"\x1b\\"].concat());
        }
        snapshot
            .control
            .extend_from_slice(&self.parser.callbacks().keyboard);
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
            screen: display_bytes(&self.published),
            base: None,
            control: Vec::new(),
            reset: false,
            expired: false,
        }
    }

    fn publish(&mut self) -> DisplayUpdate {
        let base = self.sequence;
        self.sequence = base.checked_add(1).expect("display sequence exhausted");
        let previous = std::mem::replace(&mut self.published, self.parser.screen().clone());
        let (base, screen) = if previous.size() == self.published.size() {
            let diff = self.published.state_diff(&previous);
            (Some(base), diff.into_bytes())
        } else {
            (None, display_bytes(&self.published))
        };
        let update = DisplayUpdate {
            sequence: self.sequence,
            size: self.size,
            scrolled: std::mem::take(&mut self.parser.callbacks_mut().rows),
            screen,
            base,
            control: std::mem::take(&mut self.parser.callbacks_mut().control),
            reset: std::mem::take(&mut self.reset),
            expired: false,
        };
        for row in &update.scrolled {
            self.history_len += row.len() + 24;
            self.history.push_back(row.clone());
        }
        while self.history_len > self.budget {
            self.history_evicted = true;
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
    /// The virtual display at `sequence`.
    display: Option<vt100::Parser>,
    /// Formatted virtual rows on the physical viewport since it was last
    /// cleared; an empty row has not been painted.
    painted: Vec<Vec<u8>>,
}

impl DisplayRenderer {
    pub fn new(physical: Size) -> Self {
        Self {
            physical,
            sequence: None,
            repaint: true,
            native_rows: 0,
            restore_rows: 0,
            display: None,
            painted: Vec::new(),
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

    /// Something outside the renderer wrote over the viewport; repaint even a duplicate.
    pub fn invalidate(&mut self) {
        self.repaint = true;
    }

    pub fn needs_repaint(&self) -> bool {
        self.repaint
    }

    pub fn render(&mut self, update: &DisplayUpdate) -> Result<Vec<u8>> {
        self.ensure_fits(update)?;
        let duplicate = self
            .sequence
            .is_some_and(|sequence| update.sequence <= sequence);
        let reset = update.reset && (!duplicate || !update.expired);
        if duplicate && !reset && !self.repaint {
            return Ok(Vec::new());
        }
        if let Some(sequence) = self.sequence
            && update.sequence < sequence
        {
            return Ok(Vec::new());
        }
        let mut output = b"\x1b[?6l\x1b[r\x1b[0m\x1b[?7l".to_vec();
        let native_rows = self.write_native_history(&mut output, update, reset, duplicate)?;
        let resized = self.display.as_ref().map(|display| display.screen().size())
            != Some(dimensions(update.size));
        if !duplicate {
            self.apply_display(update)?;
        }
        if reset || resized || self.repaint {
            self.clear_viewport(&mut output);
        } else {
            // Each native history row scrolled the physical viewport up by one.
            let scrolled = native_rows - self.native_rows;
            self.painted.drain(..scrolled.min(self.painted.len()));
        }
        self.write_changed_rows(&mut output);
        if !duplicate || reset {
            output.extend_from_slice(&update.control);
        }
        self.sequence = Some(update.sequence);
        self.native_rows = native_rows;
        self.restore_rows = 0;
        self.repaint = false;
        Ok(output)
    }

    fn ensure_fits(&self, update: &DisplayUpdate) -> Result<()> {
        self.physical.validate()?;
        update.size.validate()?;
        ensure!(
            self.physical.rows >= update.size.rows && self.physical.cols >= update.size.cols,
            "physical terminal is smaller than the negotiated PTY size"
        );
        Ok(())
    }

    /// Returns the native history row count after pushing this update's rows.
    fn write_native_history(
        &self,
        output: &mut Vec<u8>,
        update: &DisplayUpdate,
        reset: bool,
        duplicate: bool,
    ) -> Result<usize> {
        let mut native_rows = self.native_rows;
        if reset {
            output.extend_from_slice(b"\x1b[3J\x1b[2J\x1b[H");
            native_rows = 0;
            if update.expired {
                native_rows += self.push_history_row(output, b"[earlier output expired]")?;
            }
        } else {
            for _ in 0..self.restore_rows {
                self.scroll_physical(output);
            }
        }
        if reset || !duplicate {
            for row in &update.scrolled {
                native_rows += self.push_history_row(output, row)?;
            }
        }
        Ok(native_rows)
    }

    fn apply_display(&mut self, update: &DisplayUpdate) -> Result<()> {
        let Some(base) = update.base else {
            let (rows, cols) = dimensions(update.size);
            let mut display = vt100::Parser::new(rows, cols, 0);
            display.process(&update.screen);
            self.display = Some(display);
            return Ok(());
        };
        ensure!(
            self.sequence == Some(base),
            "display diff from {base} does not follow rendered display {:?}",
            self.sequence
        );
        let display = self
            .display
            .as_mut()
            .expect("a rendered sequence has a display");
        ensure!(
            display.screen().size() == dimensions(update.size),
            "display diff changes the display size"
        );
        display.process(&update.screen);
        Ok(())
    }

    fn clear_viewport(&mut self, output: &mut Vec<u8>) {
        output.extend_from_slice(b"\x1b[0m\x1b[H\x1b[2J");
        self.painted.clear();
    }

    /// Paints only virtual rows that differ from the physical viewport.
    fn write_changed_rows(&mut self, output: &mut Vec<u8>) {
        output.extend_from_slice(b"\x1b[0m\x1b[?7h");
        let screen = self.display.as_ref().expect("display applied").screen();
        let rows = format_rows(screen);
        self.painted.resize(rows.len(), Vec::new());
        for (index, (row, painted)) in rows.into_iter().zip(&mut self.painted).enumerate() {
            if row != *painted {
                output.extend_from_slice(format!("\x1b[{};1H", index + 1).as_bytes());
                output.extend_from_slice(&row);
                *painted = row;
            }
        }
        write_cursor_and_modes(output, screen);
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

/// Formats every cell of each virtual row, so soft wraps do not depend on the
/// larger physical width.
fn format_rows(screen: &vt100::Screen) -> Vec<Vec<u8>> {
    let (rows, cols) = screen.size();
    let format_row = |row| {
        (0..cols.get())
            .map(|col| screen.cell(row, col).expect("cell within virtual screen"))
            .filter(|cell| !cell.is_wide_continuation())
            .flat_map(format_cell)
            .collect()
    };
    (0..rows.get()).map(format_row).collect()
}

fn write_cursor_and_modes(output: &mut Vec<u8>, screen: &vt100::Screen) {
    let (_, cols) = screen.size();
    let (row, col) = screen.cursor_position();
    let col = col.min(cols.get() - 1);
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
    /// A client renders the attach snapshot before any live update, as the
    /// server's reply to Hello guarantees.
    fn attach_client(
        state: &TerminalState,
        physical: Size,
        history: usize,
    ) -> (DisplayRenderer, vt100::Parser) {
        let mut renderer = DisplayRenderer::new(physical);
        let mut outer = parser(physical, history);
        outer.process(&renderer.render(&state.snapshot(None)).unwrap());
        (renderer, outer)
    }

    const PI_SIZE: Size = Size {
        rows: 46,
        cols: 165,
    };
    const PI_EDITOR_ROW: u16 = 40;

    fn pi_chat_text(index: usize) -> String {
        let text = format!("chat-{index:03} {}", "assistant output ".repeat(10));
        let red = index * 5 % 256;
        format!("\x1b[38;2;{red};140;220m{text:<165.165}\x1b[0m")
    }
    fn pi_editor_text(text: &str) -> String {
        format!("\x1b[48;2;40;40;52m\x1b[38;5;250m{text:<165}\x1b[0m")
    }
    fn at_row(row: u16, text: &str) -> String {
        format!("\x1b[{};1H{text}", row + 1)
    }
    /// A full-width Pi-like frame: truecolor chat above a filled editor box.
    fn pi_frame() -> String {
        let chat = (0..PI_EDITOR_ROW).map(|row| at_row(row, &pi_chat_text(row.into())));
        let editor = (PI_EDITOR_ROW..PI_SIZE.rows).map(|row| at_row(row, &pi_editor_text("> ")));
        chat.chain(editor).collect()
    }
    /// Pi appends one chat row and redraws the editor below it, scrolling once.
    fn pi_append_chat(index: usize) -> String {
        let editor = vec![pi_editor_text("> "); usize::from(PI_SIZE.rows - PI_EDITOR_ROW)];
        let rows = [vec![pi_chat_text(index)], editor].concat();
        at_row(PI_EDITOR_ROW, &rows.join("\r\n"))
    }

    fn wire_bytes(update: &DisplayUpdate) -> usize {
        serde_json::to_vec(update).unwrap().len()
    }

    #[derive(Debug)]
    struct RenderCost {
        live_output: usize,
        reference_output: usize,
        live_wire: usize,
        reference_wire: usize,
    }

    /// Renders each live update beside a reference client that receives a full
    /// snapshot and repaints the whole display; both native terminals must match.
    struct RenderComparison {
        sequence: u64,
        live: DisplayRenderer,
        live_terminal: vt100::Parser,
        reference: DisplayRenderer,
        reference_terminal: vt100::Parser,
    }

    impl RenderComparison {
        fn attach(state: &TerminalState, physical: Size) -> Self {
            let (live, live_terminal) = attach_client(state, physical, 400);
            let (reference, reference_terminal) = attach_client(state, physical, 400);
            Self {
                sequence: state.snapshot(None).sequence,
                live,
                live_terminal,
                reference,
                reference_terminal,
            }
        }

        fn render(&mut self, state: &TerminalState, update: &DisplayUpdate) -> RenderCost {
            let snapshot = state.snapshot(Some(self.sequence));
            assert_eq!(
                snapshot.sequence, update.sequence,
                "compare the latest update"
            );
            self.sequence = update.sequence;
            let live = self.live.render(update).unwrap();
            self.live_terminal.process(&live);
            self.reference.invalidate();
            let reference = self.reference.render(&snapshot).unwrap();
            self.reference_terminal.process(&reference);
            assert_eq!(
                self.live_terminal.screen().contents_formatted(),
                self.reference_terminal.screen().contents_formatted()
            );
            assert_eq!(
                all_text(&mut self.live_terminal),
                all_text(&mut self.reference_terminal)
            );
            RenderCost {
                live_output: live.len(),
                reference_output: reference.len(),
                live_wire: wire_bytes(update),
                reference_wire: wire_bytes(&snapshot),
            }
        }
    }

    /// Types into one editor row of a full Pi frame and returns that update's cost.
    fn typed_editor_row_cost(physical: Size) -> RenderCost {
        let mut state = TerminalState::new(PI_SIZE, 1_000_000).unwrap();
        let mut comparison = RenderComparison::attach(&state, physical);
        let frame = state.feed(pi_frame().as_bytes()).unwrap();
        comparison.render(&state, &frame);
        let typed = at_row(PI_EDITOR_ROW + 1, &pi_editor_text("> hello"));
        let typed = state.feed(typed.as_bytes()).unwrap();
        comparison.render(&state, &typed)
    }

    #[test]
    fn one_changed_pi_row_repaints_one_native_row() {
        for physical in [PI_SIZE, size(50, 170)] {
            let cost = typed_editor_row_cost(physical);
            assert!(cost.live_output * 20 < cost.reference_output, "{cost:?}");
        }
    }

    #[test]
    fn one_changed_pi_row_sends_a_one_row_wire_update() {
        let cost = typed_editor_row_cost(PI_SIZE);
        assert!(cost.live_wire * 20 < cost.reference_wire, "{cost:?}");
    }

    #[test]
    fn display_update_after_a_missed_update_fails_explicitly() {
        let mut state = TerminalState::new(size(5, 20), 10_000).unwrap();
        let mut renderer = DisplayRenderer::new(size(5, 20));
        renderer.render(&state.snapshot(None)).unwrap();
        state.feed(b"missed").unwrap();
        let next = state.feed(b" next").unwrap();
        assert!(renderer.render(&next).is_err());
    }

    #[test]
    fn scrolled_pi_chat_repaints_only_rows_that_changed_after_the_scroll() {
        let mut state = TerminalState::new(PI_SIZE, 1_000_000).unwrap();
        let mut comparison = RenderComparison::attach(&state, size(50, 170));
        let frame = state.feed(pi_frame().as_bytes()).unwrap();
        comparison.render(&state, &frame);
        for index in 40..43 {
            let appended = state.feed(pi_append_chat(index).as_bytes()).unwrap();
            assert_eq!(appended.scrolled.len(), 1);
            let cost = comparison.render(&state, &appended);
            assert!(cost.live_output * 4 < cost.reference_output, "{cost:?}");
        }
        let text = all_text(&mut comparison.live_terminal);
        for index in 0..43 {
            assert_eq!(
                text.matches(&format!("chat-{index:03}")).count(),
                1,
                "{text}"
            );
        }
    }

    #[test]
    fn bulk_scrollback_survives_larger_physical_screens_and_dedup() {
        for physical in [size(12, 30), size(24, 50), size(40, 80)] {
            let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
            let (mut renderer, mut outer) = attach_client(&state, physical, 200);
            let update = state.feed(bulk().as_bytes()).unwrap();
            assert_eq!(update.scrolled.len(), 76);
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

    fn assert_revised_markers(outer: &mut vt100::Parser) {
        let text = all_text(outer);
        assert_markers(&text);
        assert!(!text.contains("old-"), "{text}");
        assert!(!text.contains("discard-"), "{text}");
        assert_eq!(text.matches("revised-marker-").count(), 80, "{text}");
    }

    #[test]
    fn terminal_clear_fragmented_pi_redraw_replaces_native_history() {
        let physical = size(12, 30);
        let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
        let (mut renderer, mut outer) = attach_client(&state, physical, 400);
        let first = state
            .feed(bulk().replace("marker-", "old-marker-").as_bytes())
            .unwrap();
        outer.process(&renderer.render(&first).unwrap());
        for part in [b"\x1b[2J\x1b[H\x1b[".as_slice(), b"3", b"J"] {
            let update = state.feed(part).unwrap();
            assert_eq!(update.reset, part == b"J");
            outer.process(&renderer.render(&update).unwrap());
        }
        for line in bulk()
            .replace("marker-", "revised-marker-")
            .split_inclusive('\n')
        {
            let update = state.feed(line.as_bytes()).unwrap();
            outer.process(&renderer.render(&update).unwrap());
        }
        assert_revised_markers(&mut outer);
    }

    #[test]
    fn terminal_clear_same_chunk_discards_pending_rows_before_latest_clear() {
        let physical = size(12, 30);
        let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
        let input = format!(
            "{}\x1b[2J\x1b[H\x1b[3J{}\x1b[2J\x1b[H\x1b[3J{}",
            bulk().replace("marker-", "old-marker-"),
            bulk().replace("marker-", "discard-marker-"),
            bulk().replace("marker-", "revised-marker-")
        );
        let (mut renderer, mut outer) = attach_client(&state, physical, 400);
        let update = state.feed(input.as_bytes()).unwrap();
        outer.process(&renderer.render(&update).unwrap());
        assert_revised_markers(&mut outer);
        assert!(update.reset);
        let mut restored = parser(physical, 400);
        restored.process(
            &DisplayRenderer::new(physical)
                .render(&state.snapshot(None))
                .unwrap(),
        );
        assert_revised_markers(&mut restored);
    }

    #[test]
    fn terminal_clear_reconnect_snapshots_preserve_reset_and_new_rows_only() {
        for budget in [100_000, 8_000] {
            let physical = size(12, 30);
            let mut state = TerminalState::new(size(5, 20), budget).unwrap();
            let attached = state.snapshot(None);
            let first = state
                .feed(bulk().replace("marker-", "old-marker-").as_bytes())
                .unwrap();
            let before_clear = state.feed(b"\x1b[2J\x1b[H\x1b[3").unwrap();
            let clear = state.feed(b"J").unwrap();
            assert!(clear.sequence > before_clear.sequence);
            state
                .feed(bulk().replace("marker-", "revised-marker-").as_bytes())
                .unwrap();
            for after in [first.sequence, before_clear.sequence] {
                let mut renderer = DisplayRenderer::new(physical);
                let mut outer = parser(physical, 400);
                outer.process(&renderer.render(&attached).unwrap());
                outer.process(&renderer.render(&first).unwrap());
                let snapshot = state.snapshot(Some(after));
                assert!(snapshot.sequence > clear.sequence);
                outer.process(&renderer.render(&snapshot).unwrap());
                assert_revised_markers(&mut outer);
                assert!(snapshot.reset);
            }
        }
    }

    #[test]
    fn terminal_clear_text_in_osc_and_dcs_does_not_clear_history() {
        let physical = size(12, 30);
        let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
        let (mut renderer, mut outer) = attach_client(&state, physical, 200);
        outer.process(
            &renderer
                .render(&state.feed(bulk().as_bytes()).unwrap())
                .unwrap(),
        );
        // Leave DCS open: its payload is ignored, and snapshots do not serialize it.
        for byte in b"\x1b]2;literal [3J text\x07\x1bPqliteral [3J text" {
            let update = state.feed(&[*byte]).unwrap();
            assert!(!update.reset);
            outer.process(&renderer.render(&update).unwrap());
        }
        assert_markers(&all_text(&mut outer));
        let mut restored = parser(physical, 200);
        restored.process(
            &DisplayRenderer::new(physical)
                .render(&state.snapshot(None))
                .unwrap(),
        );
        assert_markers(&all_text(&mut restored));
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

    fn expiry_state() -> TerminalState {
        let mut state = TerminalState::new(size(3, 40), 300).unwrap();
        for n in 0..12 {
            state.feed(format!("unique-{n:02}\r\n").as_bytes()).unwrap();
        }
        state
    }

    fn assert_expiry_history(state: &TerminalState, snapshot: &DisplayUpdate) {
        let physical = size(8, 40);
        let mut renderer = DisplayRenderer::new(physical);
        let mut outer = parser(physical, 100);
        outer.process(&renderer.render(snapshot).unwrap());
        let live = outer.screen().contents();
        let text = all_text(&mut outer);
        assert_eq!(
            text.matches("[earlier output expired]").count(),
            1,
            "{text}"
        );
        assert!(!live.contains("expired"), "{live}");
        assert!(!text.contains("unique-00"), "{text}");
        for n in 1..12 {
            assert_eq!(text.matches(&format!("unique-{n:02}")).count(), 1, "{text}");
        }
        assert!(renderer.render(snapshot).unwrap().is_empty());
        renderer.resize_physical(size(10, 40));
        outer.set_size(10.try_into().unwrap(), 40.try_into().unwrap());
        outer.process(&renderer.render(snapshot).unwrap());
        assert_eq!(all_text(&mut outer).trim_end(), text.trim_end());
        assert_eq!(outer.screen().contents().trim_end(), live.trim_end());
        assert!(!state.snapshot(Some(snapshot.sequence)).expired);
    }

    #[test]
    fn expiry_fresh_attach_reports_eviction_across_feeds() {
        let state = expiry_state();
        let snapshot = state.snapshot(None);
        assert!(snapshot.expired);
        assert_expiry_history(&state, &snapshot);
    }

    #[test]
    fn expiry_old_sequence_reset_is_visible_and_idempotent() {
        let state = expiry_state();
        let snapshot = state.snapshot(Some(0));
        assert!(snapshot.expired);
        assert_expiry_history(&state, &snapshot);
    }

    #[test]
    fn expiry_valid_replay_and_intentional_clear_do_not_warn() {
        let mut state = expiry_state();
        let sequence = state.snapshot(None).sequence;
        state.feed(b"\x1b[Hfresh").unwrap();
        let replay = state.snapshot(Some(sequence));
        assert!(!replay.expired);
        for part in [b"\x1b[2J\x1b[H\x1b[".as_slice(), b"3", b"J"] {
            state.feed(part).unwrap();
        }
        state.feed(b"new-a\r\nnew-b\r\nnew-c\r\nnew-d").unwrap();
        let snapshot = state.snapshot(None);
        assert!(!snapshot.expired);
        let physical = size(8, 40);
        let mut outer = parser(physical, 100);
        let mut renderer = DisplayRenderer::new(physical);
        outer.process(&renderer.render(&snapshot).unwrap());
        let text = all_text(&mut outer);
        assert!(!text.contains("expired"), "{text}");
        assert!(!text.contains("unique-"), "{text}");
        for row in ["new-a", "new-b", "new-c", "new-d"] {
            assert_eq!(text.matches(row).count(), 1, "{text}");
        }
        // An explicit non-expired reset at the same sequence still restores history.
        outer.process(b"\x1b[3J\x1b[2J\x1b[H");
        outer.process(&renderer.render(&snapshot).unwrap());
        assert_eq!(all_text(&mut outer), text);
    }

    #[test]
    fn expiry_no_eviction_has_no_native_banner() {
        let physical = size(8, 40);
        let mut state = TerminalState::new(size(3, 40), 100_000).unwrap();
        state
            .feed(b"normal-a\r\nnormal-b\r\nnormal-c\r\nnormal-d")
            .unwrap();
        let snapshot = state.snapshot(None);
        assert!(!snapshot.expired);
        let mut outer = parser(physical, 100);
        outer.process(&DisplayRenderer::new(physical).render(&snapshot).unwrap());
        let text = all_text(&mut outer);
        assert!(!text.contains("expired"), "{text}");
        for row in ["normal-a", "normal-b", "normal-c", "normal-d"] {
            assert_eq!(text.matches(row).count(), 1, "{text}");
        }
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
        let (mut renderer, mut outer) = attach_client(&state, size(12, 30), 100);
        let update = state.feed(b"ABCDEFGHIJ0123456789abcdefghij").unwrap();
        outer.process(&renderer.render(&update).unwrap());
        let rows: Vec<_> = outer.screen().rows(0, 30).collect();
        assert_eq!(rows[0], "ABCDEFGHIJ");
        assert_eq!(rows[1], "0123456789");
        assert_eq!(rows[2], "abcdefghij");
        assert!(rows[3..].iter().all(|row| row.trim().is_empty()));
    }

    #[test]
    fn disabling_mouse_and_resetting_cursor_style_restores_supported_modes() {
        let mut state = TerminalState::new(size(5, 20), 10_000).unwrap();
        let (mut renderer, mut outer) = attach_client(&state, size(10, 30), 100);
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
        let mut state = TerminalState::new(size(5, 20), 100_000).unwrap();
        let (mut renderer, mut outer) = attach_client(&state, size(12, 30), 200);
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
    fn installed_pi_cwd_and_string_terminator_preserve_visible_text() {
        let mut state = TerminalState::new(size(10, 80), 10000).unwrap();
        state
            .feed(b"before\x1b]7;file://host/project\x1b\\after")
            .unwrap();
        let snapshot = state.snapshot(None);
        assert!(
            snapshot
                .control
                .windows(b"file://host/project".len())
                .any(|bytes| bytes == b"file://host/project")
        );
        let mut outer = parser(size(10, 80), 100);
        outer.process(
            &DisplayRenderer::new(size(10, 80))
                .render(&snapshot)
                .unwrap(),
        );
        assert!(outer.screen().contents().contains("beforeafter"));
    }

    #[test]
    fn pi_line_resets_preserve_long_bulk_text_and_native_history() {
        let mut state = TerminalState::new(size(16, 30), 100000).unwrap();
        let text = format!(
            "\x1b]133;A\x07{}\x1b]133;B\x07\x1b]133;C\x07",
            bulk().replace("\r\n", "\x1b[0m\x1b]8;;\x07\r\n")
        );
        let (mut renderer, mut outer) = attach_client(&state, size(30, 60), 200);
        let update = state.feed(text.as_bytes()).unwrap();
        outer.process(&renderer.render(&update).unwrap());
        assert_markers(&all_text(&mut outer));
    }

    #[test]
    fn pi_keyboard_startup_and_cleanup_negotiate_without_poisoning_output() {
        let mut state = TerminalState::new(size(24, 80), 10000).unwrap();
        let startup = state.feed(b"\x1b[?2004h\x1b[>7u\x1b[?u\x1b[c").unwrap();
        assert!(startup.control.is_empty());
        assert_eq!(state.drain_responses(), b"\x1b[?0u\x1b[?1;2c");
        let keyboard = state.feed(b"\x1b[>4;2m").unwrap();
        assert_eq!(keyboard.control, b"\x1b[>4;2m");
        assert!(
            state
                .snapshot(None)
                .control
                .windows(7)
                .any(|part| part == b"\x1b[>4;2m")
        );
        state.feed(b"\x1b[?2004l\x1b[<u\x1b[>4;0m").unwrap();
    }

    #[test]
    fn pi_startup_color_queries_remain_unanswered_without_poisoning_output() {
        let mut state = TerminalState::new(size(5, 20), 10_000).unwrap();
        for sequence in [
            b"\x1b[?2031h".as_slice(),
            b"\x1b[?996n",
            b"\x1b]11;?\x07",
            b"\x1b[?2031l",
        ] {
            for byte in sequence {
                let update = state.feed(&[*byte]).unwrap();
                assert!(update.control.is_empty());
                assert!(state.drain_responses().is_empty());
            }
        }
        let (mut renderer, mut outer) = attach_client(&state, size(12, 30), 200);
        let update = state.feed(bulk().as_bytes()).unwrap();
        outer.process(&renderer.render(&update).unwrap());
        assert_markers(&all_text(&mut outer));
    }

    #[test]
    fn pi_startup_progress_notifications_pass_through_without_becoming_queries() {
        let mut state = TerminalState::new(size(5, 20), 10_000).unwrap();
        for (sequence, expected) in [
            (b"\x1b]9;4;3\x07".as_slice(), b"\x1b]9;4;3\x1b\\".as_slice()),
            (
                b"\x1b]9;4;0;\x07".as_slice(),
                b"\x1b]9;4;0;\x1b\\".as_slice(),
            ),
        ] {
            let mut control = Vec::new();
            for byte in sequence {
                control.extend(state.feed(&[*byte]).unwrap().control);
            }
            assert_eq!(control, expected);
            assert!(state.drain_responses().is_empty());
        }
        // Unknown OSC 9 commands are still unsupported, not blanket-ignored.
        assert!(state.feed(b"\x1b]9;unknown\x07").is_err());
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
        state.feed(b"valid").unwrap();
        let before = state.snapshot(None);
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

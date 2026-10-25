# Unified Pi session design notes

Status: Rust prototype implemented through `28c9a90`; product acceptance and deployment remain incomplete. The [specification](../../specs/unified-pi-session.md) owns requirements.

## Observed loss boundary

The fixed-size native diagnostic uses an actual Pi assistant-message component, Rust detach, Mosh client/server, and a headless terminal at 165 columns × 46 rows. A bulk update contains 80 uniquely numbered lines.

| Path | Ordered markers received | Scrollback rows |
| --- | --- | --- |
| Pi → detach → terminal | 000–079 | 41 |
| Pi → detach → Mosh → terminal | 034–079 | 0 |

The missing prefix is already absent from the captured Mosh client output. This is not proof of a Pi widget row-count error. A separate slow incremental update retained all 80 lines, so output timing matters. The user's screenshot is cropped horizontally; its right edge does not establish clipping.

The reproduction is committed as `e34f683`; local diagnostic evidence is under `/var/tmp/pi-resize-fix/long-live-red/`, including `direct-vs-mosh.json`. No repair of that stack has been demonstrated.

## Mosh source boundary

Upstream source was inspected at `mosh-1.4.0`, commit `bc73a26316ede2a79259d859f8ee309b32412420`, cloned into `~/Repos/mosh`.

- `src/frontend/mosh-server.cc`, `serve()`, reads PTY output and calls `terminal.act()`.
- `src/statesync/completeterminal.cc`, `Complete::act()`, parses those bytes into terminal state; `Complete::diff_from()` computes screen differences.
- `src/frontend/stmclient.cc`, `STMClient::output_new_frame()`, renders received framebuffer state.
- `src/frontend/terminaloverlay.*` implements predictive local echo and connection-notification overlays.
- `src/network/network.cc`, `Connection::recv_one()`, handles a changed peer address, not durable output retention or dead-server restoration.

Tokei counted 9,560 production C++/header code lines across 63 files, excluding comments, blanks, tests, fuzz targets, examples, and generated protobuf output. This is not a Rust implementation estimate.

## Implemented prototype

`remote-agent-rs` is one Rust binary with local client, SSH control, and detached Linux session-server commands. Python controller/workers, detach and Mosh remain operational. Opt-in Rust deployment and real attachment were verified on agent-server for `osso` and `allison` at `fdaa0d6`; legacy sessions were preserved. See the [build and CLI guide](../../../README.md#rust-prototype).

SSH uses existing private host/project maps and returns a ticket containing the server address, certificate and authentication token. Quinn carries length-prefixed JSON messages over an ordered bidirectional QUIC stream. The client trusts the ticket certificate; the server checks the token. Byte fields require base64 strings. The server limits the unauthenticated frame to 8192 bytes from its length header before payload allocation; authenticated frames allow at most 80 MiB. The Linux server owns Pi's PTY independently of attachments. There is no predictive echo or Mosh wire compatibility.

### Output and retention

The server keeps one authoritative, persistent `vt100` parser. Each PTY output chunk produces **every scrolled row**, a `vt100` state diff from the previously published display (`base` names that sequence; a virtual resize sends the complete display instead) and supported control events. This is ordered QUIC delivery, not Mosh's screen-only synchronization or raw PTY-byte replay. The native client writes captured rows into the outer terminal's scrollback, applies the display to its own visible-display parser, then repaints only virtual rows that differ from the rows it painted, shifted by each pushed history row. Reset, virtual resize and repaint requests clear the viewport first. A diff whose base the client has not rendered fails the attachment; reconnect receives a complete snapshot. Diffs deferred while the physical terminal is smaller are concatenated. The client parser does not own the application's hidden VT state.

History rows and the replay-update journal are separate in-memory queues, each capped by the configured accounting budget. The default and CLI maximum are individually 16 MiB; `start --history-bytes N` accepts 1–16777216, but the budget must fit the current display checkpoint. Accounting includes fixed/per-row overhead, not a whole-process memory limit. A live update includes all rows from that chunk even when it exceeds retention. There is no disk output store: `src/history.rs` and `history.jsonl` were deleted by user decision. Byte payloads use base64 rather than numeric arrays.

Reconnect supplies the last rendered sequence. Retained journal updates supply missing rows/control events with the current frame. Fresh attachment or an unavailable replay position receives retained history plus the current frame with reset set. Expired history is marked, including a fresh attachment after retained rows were evicted; the client inserts `[earlier output expired]` into native scrollback. Reset clears existing outer-terminal scrollback and is idempotent for duplicate delivery. The parser and retained output exist only in the live server and are lost when it exits. `logs` works only for running sessions: the control process connects to the server's local QUIC port, sends a one-shot authenticated `Logs { token }` frame and receives the snapshot. It does not join the session, change PTY dimensions or repaint the caller's terminal.

Alternate-screen applications are unsupported. Unknown VT callbacks, clipboard-read queries and application-requested resize can fail the parser; the library also silently ignores some unsupported modes/strings. This is neither general terminal compatibility nor a terminal sanitizer. Earlier offline native Pi capability tests passed within the boundaries below; full VT compatibility and current-revision acceptance remain open.

### Clients, input and images

Clients have separate identities, output positions and input sequences. The server serializes input and ignores already applied input sequences. Shared PTY rows and columns are independently the minima across clients. Leaving the last attachment retains the previous size. Five consecutive failed reconnect attempts end the attachment. Any authenticated display frame resets the failure count. Backoff ceilings are 500/1000/2000/4000 ms with 90–100% jitter. Native outage and NAT port migration tests cover the boundaries recorded below, not every network condition.

Reconnect, control, clipboard and server notices use the bottom physical terminal row with save/restore cursor, autowrap disabled and no line feed. The next display repaints over the notice, including a duplicate reconnect snapshot. This fixed the observed duplication of two rows after reconnect.

Outside bracketed text paste, the client intercepts Ctrl+V and recognized Kitty Ctrl+V encodings. It reads the initiating client's clipboard on demand and sends PNG bytes as a sequenced image action. The server creates a unique private image file, then inserts its path as one bracketed paste. No shared clipboard cache or manual staging command is used. Linux selects `wl-paste` for Wayland or `xclip` for X11; macOS uses `osascript` with AppKit and base64 PNG output. Empty/failed clipboard reads report an error before an image is sent. Earlier offline Pi image-editor consumption passed; current-revision and simultaneous real-client acceptance remain open.

Pi login uses interactive SSH and `pi login PROVIDER`, not a new credential store. Earlier offline CLI tests covered invalid-provider login, not full OAuth or credentials. Native `__as-user` resolves passwd identity/home and enters that home with a clean environment. Account isolation, launch variables, service ownership and cancellation policy have one contract in [configuration](../../specs/configuration.md#rust-account-and-launch-policy). Real cross-user attachment was verified for both accounts at `fdaa0d6`.

The child environment identifies `TERM_PROGRAM=remote-agent-rs`, with xterm-compatible true color, and removes inherited Ghostty/Kitty/WezTerm/Warp/iTerm graphics indicators. Pi must detect the virtual endpoint's capabilities, not the physical terminal that launched SSH. File-based image paste remains independent of inline Kitty/iTerm graphics, which the prototype does not implement.

A generated conversation uses Pi's `--session-id UUID`, which opens the existing conversation if one exists in the project. Explicit `--resume UUID` uses `--session UUID`; these resume semantics are unchanged. UUID-conflict handling is excluded. Earlier isolated-HOME Pi acceptance was offline and did not submit a model prompt.

### Pi keyboard negotiation and detach

The virtual endpoint answers Pi's keyboard/cursor/device queries itself rather than broadcasting queries to all clients. Kitty push/pop requests do not enable physical Kitty mode; the endpoint reports zero Kitty flags and forwards Pi's xterm `modifyOtherKeys` setting. The local input decoder recognizes image paste and detach in legacy, Kitty, and xterm encodings while leaving bracketed-paste content literal.

Installed Pi's current-directory metadata (`OSC 7`) is retained for fresh snapshots; its string terminator does not poison the virtual display. Pi's plain-text row reset (`OSC 8` close) and advisory message-zone markers (`OSC 133 A/B/C`) do not poison the virtual display. The prototype does not preserve native shell-zone navigation metadata across its reframed screen; visible message text and scrollback are separate acceptance targets.

Local detach is monitored independently of the network loop, including connection establishment and reconnection. Network reads have a dedicated task so a terminal tick or input event cannot cancel a partially read protocol frame. Attachment cleanup aborts that reader, closes its connection, and restores local terminal modes and attributes.

## Cargo dependency inventory

This is the single justification inventory for direct `Cargo.toml` dependencies. `Cargo.lock` records resolved versions.

| Dependency | Purpose |
| --- | --- |
| `anyhow` | Contextual errors and explicit validation failures. |
| `base64` | Decode macOS clipboard PNG output and encode transport byte payloads. |
| `vte` | Observe CSI 3 J with a real parser, preserving clear-history ordering alongside the authoritative vt100 parser. |
| `clap` (`derive`) | Parse public CLI and hidden control/server commands. |
| `libc` | Unix PTY, termios, polling, process groups, signals and sizing. |
| `quinn` (`runtime-tokio`, `rustls-ring`) | Ordered QUIC client/server streams with Tokio and TLS integration. |
| `rcgen` | Generate the per-server certificate returned through SSH. |
| `rustls` (`ring`, `std`) | TLS certificate/key types and client trust configuration. |
| `serde` (`derive`) | Serialize typed control, launch, ticket and display messages. |
| `serde_json` | Encode wire frames, maps, private state and CLI replies. |
| `subtle` | Constant-time attachment-token comparison. |
| `tokio` (`full`) | Async entry point, I/O, channels, tasks, timers and Unix signals. |
| `uuid` (`v4`, `serde`) | Generate session/client/attachment/token/file identities; current records use strings, so typed UUID serialization is not needed by those records. |
| `vt100` | Persistent server parsing with scroll callbacks and client display/cell formatting. |
| `tempfile` (development only) | Isolated directories and executable/process fixtures in tests. |

### Session service lifecycle

The repository-root `remote-agent-rs@.service` template is installed by
`deploy.sh --rust` into every server account's `~/.config/systemd/user/`, followed
by `systemctl --user daemon-reload`. Each session uses the enabled user unit
`remote-agent-rs@NAME.service` with `Slice=agents.slice` (the user slice).
Its launch directive is:

```text
ExecStart=%h/.local/bin/remote-agent-rs __serve --state %h/.local/state/remote-agent-rs/sessions/%i
```

Server output appends to the session's `server.log`; this is diagnostic output,
not retained Pi terminal history. `start` runs `systemctl --user enable --now`.
`cancel` runs `systemctl --user disable --now`, waits for the server PID to exit,
then runs the project's `pi_cancel_executable` hook with the conversation ID.
Repeating cancellation's unit stop is a no-op, allowing retry after a failed hook.
Systemd-run scopes, `scope.json` and InvocationID records were removed.

Server identity uses exact argv `__serve --state <absolute state path>`.
It no longer compares executable inodes, so a session started by an older
installed binary remains manageable after an upgrade.

Enabled units return when the user manager starts at boot, which requires
linger. On agent-server, `osso` has `Linger=yes`; `allison` linger was enabled
with user approval on 2026-10-25. The restarted server launches Pi with the same
`launch.json`. Conversation, session name, project and account return; running
jobs, an in-progress model turn and unsent editor text do not. Output retention
starts empty because it is in memory only. An actual host reboot was not tested.

An initial prompt (`start --prompt-file`) applies to the first launch only: the
server writes it to `prompt.txt` and removes it from `launch.json` before
launching Pi, so a restart neither fails on the existing file nor submits the
prompt again.

Each session server binds its own random UDP port for QUIC. A client-side
firewall must allow UDP to the server host on any port; on aso, appfw has one
rule for this in `~/.config/appfw/rules.d/remote-agent-rs.toml`.

`deploy.sh --rust` preserves each account's maps and installs private immutable
hash-addressed binaries through a stable link. Python remains the default.
Installation does not transfer a legacy PTY. Legacy detach has no SCM_RIGHTS
handoff; existing sessions must not be migrated or cancelled without approval.

## Evidence and remaining acceptance

The main thread supplied this proof for the changes after `eb27c84` through
`fdaa0d6`, verified 2026-10-25. This docs-only update ran no tests, CI queries,
deployment or operational checks. These results do not establish full product
acceptance.

| Boundary | Evidence | Remaining gap |
| --- | --- | --- |
| Native lifecycle/restore | `tests/test_rust_attach.py` 5/5, including simulated-reboot restore; fixture SSH/systemd with real PTY/QUIC/Pi TUI components | Actual host reboot not performed |
| Network/reconnect | `tests/test_rust_network.py` 3/3, including reconnect-notice regression and native outage/NAT migration coverage; `tests/test_rust_retry_budget.py` six outages on one client over 193 seconds | Not all network conditions or complete real roaming acceptance |
| Upgrade/deployment | `tests/test_rust_upgrade.py`; `tests/test_rust_deploy.py` 8/8; Rust control/client/server unit tests | No legacy PTY handoff or migration proof |
| Real agent-server, both accounts | At `fdaa0d6`, `osso` and `allison` units enabled and active in user `agents.slice`; correct UID, cwd, PTY 165×46 and `CARGO_BUILD_JOBS`; real SSH/QUIC attach, typed input reaches Pi, clean detach | Installed macOS-to-Linux: see the macOS row; simultaneous real clients: see the paste and detach rows |
| Real restore/cancel | Whole-unit SIGKILL then `systemctl --user start` restored the same conversation; osso's resumed transcript visible again; cancel left unit disabled/inactive | Actual reboot deliberately not performed because it would stop legacy sessions; jobs, model turn and editor text do not return |
| Legacy preservation | Legacy sessions, maps, credentials and legacy code remained byte/PID-identical | Not authorization to migrate them |
| Legacy migration: `pi-rust-repin` (2026-10-25, user-requested) | SIGTERM to the legacy Pi only (exit 0 in 0.6 s); the legacy dtach scope kept its detached jobs (cargo test, bwrap sandboxes); record archived to `~/.local/state/remote-agent-migrated/`, no `cancel`; `remote-agent-rs start pi-rust pi-rust-repin --resume f8484104…` gave an enabled, active unit with the same argv and cwd and a single transcript file, and Pi resumed its goal and child agents | The in-progress turn was interrupted with user approval |
| Legacy migration: all remaining sessions (2026-10-25, user-requested, interrupts approved) | Same procedure for osso `world-of-osso`, `wow-ui-sim`, `clank` and allison `hhh`, `HH_bbtautau`, `mattermost-cli` (tmux era, launched with `--session-id`). Every legacy Pi exited 0; clank via `docker stop` of its conversation container, then relaunched through `session.py --session`. All seven units enabled and active, with `--session UUID`, the project cwd and the unit cgroup; `logs` shows each resumed title and goal; no legacy records remain for either account. Real-TTY `remote-agent [--user allison] attach NAME` at 165×46 displayed all seven, Ctrl+\ detached each with exit 0 in 0.15–0.5 s with terminal attributes restored, and every server/Pi PID was unchanged afterwards | Interrupted turns lost their foreground tool processes (world-of-osso scope 80→29, wow-ui-sim 13→0); clank's in-container jobs ended with its container; allison's dead tmux pane `remote-agent-mattermost-cli` remains |
| macOS | GitHub run `40226091616` passed the Ubuntu and macOS jobs (native clipboard fixture, PTY and unit tests) at `fdaa0d6`; run `40230014481` passed both at `c06678a`. Installed on allison's arm64 macOS 26.6.2 laptop (rustc 1.92) at `7831e16`: `./deploy.sh --rust` built the macOS client as `remote-agent` and the Linux server on agent-server from the bundled source; `list` over SSH worked; a real-PTY `attach mattermost-cli` at 120×40 over Wi-Fi LAN QUIC drew the Pi UI, Ctrl+\ detached with exit 0 and the terminal modes were reset; run twice. Reported by the Mac's own Pi session, not re-run here | After detach, macOS `stty -g` differs only by PENDIN (`lflag` 5cb → 200005cb); macOS image paste not exercised |
| Display diffs (`c06678a`) | Native attach/resize/network suites pass; client output bytes across the same stages fell from 4,787,949 to 2,138,492 (45%); first screen after attach 230 KB → 39 KB. Real agent-server attach: typing after attach wrote about 9 KB instead of about 90 KB per update; client viewport matched the server `logs` snapshot; unit acceptance passed again for both accounts | Fresh attach and reconnect replay still send the complete retained backlog and display, by design |
| Detach and smallest size (`0c6df6d`) | Before: Ctrl+\ exited before QUIC CONNECTION_CLOSE left, so the server logged `connection lost: timed out` and a detached smaller client kept the shared PTY small until the idle timeout. Now the client closes its endpoint and waits for drain (500 ms bound) after restoring the terminal. Native RED/GREEN in `test_physical_growth_with_second_client_pinning_pty_minimum`; real agent-server: Pi PTY 40×120 with both clients, 46×165 after the smaller one detached, server logs `closed by peer: client detached`. All native suites and `cargo test` pass at `0c6df6d` | Offline exit waits at most 500 ms more |
| Image paste (`c06678a`, rerun at `0c6df6d`) | Real agent-server Pi, two simultaneous Linux clients (165×46 and 120×40); a private headless cage clipboard supplied a different PNG to each Ctrl+V; server images byte-identical (SHA-256); both paths in Pi's editor on both clients; bracketed text containing Ctrl+V stayed text; empty clipboard gave a notice with no image or editor change | macOS clipboard only in CI fixture; model call with the image not exercised |
| Earlier Pi/renderer proof | Offline CLI at `121e38e`, native Pi TUI at `fca936a`, PTY/QUIC 80-marker fixtures, resize work at `6267448`/`5b8f341` | Full OAuth/model call, per-display geometry and complete VT coverage remain open |

The existing fixed-size Mosh 80-marker regression remains RED; Rust work does
not repair it. Root process inventories at
`/var/tmp/pi-resize-fix/rust-production/before-{osso,allison}.json` are
preservation baselines, not proof of live handoff.

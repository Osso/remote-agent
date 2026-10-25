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

`remote-agent-rs` is one Rust binary with local client, SSH control, and detached Linux session-server commands. Python controller/workers, detach and Mosh remain operational; this prototype has not been deployed. See the [build and CLI guide](../../../README.md#rust-prototype).

SSH uses existing private host/project maps and returns a ticket containing the server address, certificate and authentication token. Quinn carries length-prefixed JSON messages over an ordered bidirectional QUIC stream. The client trusts the ticket certificate; the server checks the token. Byte fields require base64 strings. The server limits the unauthenticated frame to 8192 bytes from its length header before payload allocation; authenticated frames allow at most 80 MiB. The Linux server owns Pi's PTY independently of attachments. There is no predictive echo or Mosh wire compatibility.

### Output and retention

The server keeps one authoritative, persistent `vt100` parser. Each PTY output chunk produces **every scrolled row**, the current formatted frame and supported control events. This is ordered QUIC delivery, not Mosh's screen-only synchronization or raw PTY-byte replay. The native client writes captured rows into the outer terminal's scrollback, then repaints the shared viewport. Its temporary display parsers do not own the application's hidden VT state.

History rows and the replay-update journal are separate in-memory queues, each capped by the configured accounting budget. The default and CLI maximum are individually 16 MiB; `start --history-bytes N` accepts 1–16777216, but the budget must fit the current display checkpoint. Accounting includes fixed/per-row overhead, not a whole-process memory limit. A live update includes all rows from that chunk even when it exceeds retention. A private JSONL disk journal uses explicit reader/writer locks and separately enforces the configured serialized-byte limit; ordinary deltas append, while reset or overflow writes an atomic checkpoint and trims oldest archived rows if needed. Byte payloads use base64 rather than numeric arrays.

Reconnect supplies the last rendered sequence. Retained journal updates supply missing rows/control events with the current frame. Fresh attachment or an unavailable replay position receives retained history plus the current frame with reset set. Expired history is marked, including a fresh attachment after retained rows were evicted; the client inserts `[earlier output expired]` into native scrollback. Reset clears existing outer-terminal scrollback and is idempotent for duplicate delivery. `history.jsonl` persists bounded checkpoints/deltas, not hidden parser state; it has no per-delta fsync or power-loss guarantee. The parser stays authoritative in the live server; archived display data cannot restore a dead server or PTY. `logs` reconstructs plain text without repainting the caller's terminal.

Alternate-screen applications are unsupported. Unknown VT callbacks, clipboard-read queries and application-requested resize can fail the parser; the library also silently ignores some unsupported modes/strings. This is neither general terminal compatibility nor a terminal sanitizer. Earlier offline native Pi capability tests passed within the boundaries below; full VT compatibility and current-revision acceptance remain open.

### Clients, input and images

Clients have separate identities, output positions and input sequences. The server serializes input and ignores already applied input sequences. Shared PTY rows and columns are independently the minima across clients. Leaving the last attachment retains the previous size. Reconnect retries exist; loopback tests do not establish real network roaming or all retry/error cases.

Outside bracketed text paste, the client intercepts Ctrl+V and recognized Kitty Ctrl+V encodings. It reads the initiating client's clipboard on demand and sends PNG bytes as a sequenced image action. The server creates a unique private image file, then inserts its path as one bracketed paste. No shared clipboard cache or manual staging command is used. Linux selects `wl-paste` for Wayland or `xclip` for X11; macOS uses `osascript` with AppKit and base64 PNG output. Empty/failed clipboard reads report an error before an image is sent. Earlier offline Pi image-editor consumption passed; current-revision and simultaneous real-client acceptance remain open.

Pi login uses interactive SSH and `pi login PROVIDER`, not a new credential store. Earlier offline CLI tests covered invalid-provider login, not full OAuth or credentials. Native `__as-user` resolves passwd identity/home and enters that home with a clean environment. Account isolation, launch variables, scope ownership and cancellation policy have one contract in [configuration](../../specs/configuration.md#rust-account-and-launch-policy); installed cross-user acceptance remains open.

The child environment identifies `TERM_PROGRAM=remote-agent-rs`, with xterm-compatible true color, and removes inherited Ghostty/Kitty/WezTerm/Warp/iTerm graphics indicators. Pi must detect the virtual endpoint's capabilities, not the physical terminal that launched SSH. File-based image paste remains independent of inline Kitty/iTerm graphics, which the prototype does not implement.

A newly generated conversation uses Pi's `--session-id` creation flag. Explicit `--resume` uses `--session` to open the requested conversation; a missing conversation must not silently become a new one. Installed-Pi acceptance runs offline in an isolated HOME and never submits a model prompt.

### Pi keyboard negotiation and detach

The virtual endpoint answers Pi's keyboard/cursor/device queries itself rather than broadcasting queries to all clients. Kitty push/pop requests do not enable physical Kitty mode; the endpoint reports zero Kitty flags and forwards Pi's xterm `modifyOtherKeys` setting. The local input decoder recognizes image paste and detach in legacy, Kitty, and xterm encodings while leaving bracketed-paste content literal.

Installed Pi's current-directory metadata (`OSC 7`) is retained for fresh snapshots; its string terminator does not poison the virtual display. Pi's plain-text row reset (`OSC 8` close) and advisory message-zone markers (`OSC 133 A/B/C`) do not poison the virtual display. The prototype does not preserve native shell-zone navigation metadata across its reframed screen; visible message text and scrollback are separate acceptance targets.

Local detach is monitored independently of the network loop, including connection establishment and reconnection. Network reads have a dedicated task so a terminal tick or input event cannot cancel a partially read protocol frame. Attachment cleanup aborts that reader, closes its connection, and restores local terminal modes and attributes.

## Cargo dependency inventory

This is the single justification inventory for direct `Cargo.toml` dependencies. `Cargo.lock` records resolved versions.

| Dependency | Purpose |
| --- | --- |
| `anyhow` | Contextual errors and explicit validation failures. |
| `base64` | Decode macOS clipboard PNG output and encode transport/journal byte payloads. |
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

### Current client and lifecycle work

`6267448` fixes deferred reset/control delivery and retry handling. `5b8f341`
adds analyzer coverage and two native resize cases. Per-display geometry remains
pending; shared-PTY minimum dimensions do not prove correct rendering at every
physical client size. Pending main/protocol/server/PTY and attach-test changes
are not a completed acceptance revision.

`deploy.sh --rust` selects parallel installation, preserving both accounts' maps,
private immutable hash versions and a stable link with compare-and-swap checks.
Python remains the default. No operational Rust deployment or live Pi handoff
has occurred. Legacy detach has no SCM_RIGHTS transfer mechanism; do not infer
that installing the new binary transfers an existing PTY. Existing Pi processes
must not be migrated or cancelled without authorization.

## Evidence and remaining acceptance

Evidence below was supplied for this bounded docs audit on 2026-10-25; no tests,
CI queries or operational commands were run here. It is preliminary,
revision-scoped evidence, not fresh acceptance of pending source.

| Boundary | Evidence | Remaining gap |
| --- | --- | --- |
| Real Pi offline CLI | Two tests at `121e38e`: new conversation, image editor, detach and invalid-provider login CLI | Current-revision rerun; no full OAuth, credentials, prompt submission or model call |
| Native Pi TUI | Two tests at `fca936a`: bulk output, revised output and widgets with real Pi offline | Current-revision rerun, real network roaming and installed attachment |
| macOS native client | GitHub run `40206416647` macOS job SUCCESS, including native clipboard, PTY and unit tests | Current CI and macOS-to-Linux installed acceptance; not “macOS untested” |
| Ubuntu CI | Same run failed a fixture readiness race; local fix `81021f2` reported | Fresh current-revision CI, not an all-platform green claim |
| Auth framing/network target | One targeted GREEN each reported in `/var/tmp/pi-resize-fix/rust-server/auth-*-green.log` | These narrow passes do not establish broad protocol acceptance |
| Replay and resize | Earlier PTY/QUIC fixtures preserve all 80 markers; `6267448` client fixes and `5b8f341` analyzer/two native resize cases added | Per-display geometry pending; current integration reruns required |
| Retention | Bounded JSONL, atomic checkpoints, locks/readers and explicit client expiry marker implemented | Current end-to-end replay/reset/control/retry proof |
| Account/lifecycle/deployment | Native account routing, owned scope cancellation and opt-in install implemented with fixtures | Actual two-account Rust deployment and installed acceptance; no operational deployment yet |

The existing fixed-size Mosh 80-marker regression remains RED; Rust work does not
repair it. Root process inventories for both accounts were captured at
`/var/tmp/pi-resize-fix/rust-production/before-{osso,allison}.json`, including
real Pi/master/wrapper PIDs. These are preservation baselines, not proof of
migration, deployment or live handoff.

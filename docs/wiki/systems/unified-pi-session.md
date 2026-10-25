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

SSH uses existing private host/project maps and returns a ticket containing the server address, certificate and authentication token. Quinn carries length-prefixed JSON messages over an ordered bidirectional QUIC stream. The client trusts the ticket certificate; the server checks the token. The Linux server owns Pi's PTY independently of attachments. There is no predictive echo or Mosh wire compatibility.

### Output and retention

The server keeps one authoritative, persistent `vt100` parser. Each PTY output chunk produces **every scrolled row**, the current formatted frame and supported control events. This is ordered QUIC delivery, not Mosh's screen-only synchronization or raw PTY-byte replay. The native client writes captured rows into the outer terminal's scrollback, then repaints the shared viewport. Its temporary display parsers do not own the application's hidden VT state.

History rows and the replay-update journal are separate in-memory queues, each capped by the configured accounting budget. The default and CLI maximum are individually 16 MiB; `start --history-bytes N` accepts 1–16777216. Accounting includes fixed/per-row overhead, not a whole-process memory limit. A live update includes all rows from that chunk even when it exceeds retention. Current frame storage, transient messages and JSON disk size are not capped by that queue budget.

Reconnect supplies the last rendered sequence. Retained journal updates supply missing rows/control events with the current frame. Fresh attachment or an unavailable replay position receives retained history plus the current frame with reset set; unavailable positions also set expired. Reset clears existing outer-terminal scrollback. `history.json` persists the current history/display snapshot, not the journal or serialized hidden parser state. The parser stays authoritative in the live server; the snapshot cannot restore a dead server or PTY. The client does not yet display a distinct expired-history warning.

Alternate-screen applications are unsupported. Unknown VT callbacks, clipboard-read queries and application-requested resize can fail the parser; the library also silently ignores some unsupported modes/strings. This is neither general terminal compatibility nor a terminal sanitizer. Actual Pi capability coverage remains unproven.

### Clients, input and images

Clients have separate identities, output positions and input sequences. The server serializes input and ignores already applied input sequences. Shared PTY rows and columns are independently the minima across clients. Leaving the last attachment retains the previous size. Reconnect retries exist; loopback tests do not establish real network roaming or all retry/error cases.

Outside bracketed text paste, the client intercepts Ctrl+V and recognized Kitty Ctrl+V encodings. It reads the initiating client's clipboard on demand and sends PNG bytes as a sequenced image action. The server creates a unique private image file, then inserts its path as one bracketed paste. No shared clipboard cache or manual staging command is used. Linux selects `wl-paste` for Wayland or `xclip` for X11; macOS uses `osascript` with AppKit and base64 PNG output. Empty/failed clipboard reads report an error before an image is sent. Cleanup and actual Pi editor consumption remain unverified.

Pi login uses interactive SSH and `pi login PROVIDER`, not a new credential store. Actual provider login has not been tested. Prototype `--user` routing uses `authsudo` and `/home/USER`; it does not establish Python's account-directory behavior or cross-user acceptance.

## Cargo dependency inventory

This is the single justification inventory for direct `Cargo.toml` dependencies. `Cargo.lock` records resolved versions.

| Dependency | Purpose |
| --- | --- |
| `anyhow` | Contextual errors and explicit validation failures. |
| `base64` | Decode macOS clipboard PNG output. |
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

## Evidence and remaining acceptance

Reported targeted development passes through `28c9a90`: 2 native server tests; earlier 8 configuration/control tests, 9 local tests and 11 terminal tests. This docs audit inspects source only and does not rerun or broaden that proof.

| Boundary | Evidence | Remaining gap |
| --- | --- | --- |
| Bulk output/reconnect | Real Linux PTY + loopback QUIC + scripted child: all 80 markers once, offline replay, surviving child | Actual Pi, installed SSH path, real interruption/address change |
| Simultaneous clients | Scripted PTY: minimum size, join/leave changes, distinct private image files | Actual concurrent Pi editing/paste and cross-user attachment |
| Terminal reconstruction | Targeted fragmentation, scrollback, replay/expiry, resize, wraps and query tests | Actual Pi VT output; alternate screen and unknown callbacks unsupported |
| Local input/clipboard | Key framing, bracketed paste, restoration and injectable provider fixtures | Real Linux clipboard-to-Pi paste, macOS build/runtime, provider login |
| Rollout | Source and fixture proof only | Full integration acceptance, safe migration and deployment; no full-suite claim |

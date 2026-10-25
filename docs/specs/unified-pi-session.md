# Unified Rust Pi sessions

The user-approved product scope is a single Rust project replacing the needed remote-agent, detach, and Mosh behavior for Pi sessions. The Rust prototype is under implementation; full product acceptance and deployment remain incomplete. The existing implementations remain in service. See [design notes](../wiki/systems/unified-pi-session.md).

## What it must do

### Confirmed scope

- [ ] Use one Rust project for session management, detached PTY ownership, and remote attachment.
- [ ] Support Pi only; replace both transport endpoints without Mosh wire compatibility.
- [ ] Do not provide predictive local echo.
- [ ] Support Linux and macOS clients, with Linux servers.
- [ ] Retain Pi provider login.
- [ ] Paste a local clipboard image into remote Pi without a separate manual `paste` command.
- [ ] Keep bounded, configurable output history; clearly report expired output when a requested replay position is no longer retained.
- [ ] Allow simultaneous attached clients; size the shared Pi PTY to the smallest attached terminal dimensions.

### Session and transport acceptance

Full scope is approved. Unchecked items remain product acceptance gaps; they do not mean the prototype has no implementation.

- [ ] Keep Pi and its PTY alive when a client detaches or its network connection fails.
- [ ] Preserve all ordered output within the agreed retention boundary, including updates longer than the viewport; do not substitute the latest screen for output history.
- [ ] Reconnect after a network interruption or address change without losing or duplicating acknowledged terminal output.
- [ ] Preserve input, terminal dimensions, clean detach, and local terminal restoration.
- [ ] Retain the needed session commands and host/account/project routing, with user-specific credentials and configuration kept separate.

### Image-paste acceptance

- [ ] The normal Pi image-paste action obtains the current local image without a separate upload command.
- [ ] Make image data available before Pi consumes the paste; do not paste a stale image after an empty clipboard or transfer failure.
- [ ] Preserve ordinary text paste and its terminal input framing.
- [ ] Transfer clipboard contents on demand, not by continuously synchronizing the clipboard.
- [ ] Associate each paste with the initiating client's clipboard and intended session/account, including simultaneous attachments; do not let another client's image replace it.

## How it works

- [Design notes and observed failure boundary](../wiki/systems/unified-pi-session.md).
- [Cargo dependency justifications](../wiki/systems/unified-pi-session.md#cargo-dependency-inventory).
- [Current account/project configuration](configuration.md).
- [Current session preservation constraints](session-backends.md).

## Implementation inventory

The Rust prototype uses one package, `remote-agent-rs`:

- `src/main.rs`, `src/config.rs`, `src/control.rs`: CLI/control dispatch, existing configuration maps, private session records and process identity.
- `src/server.rs`, `src/pty.rs`: QUIC endpoint, persistent Pi PTY, serialized client input and image-file insertion.
- `src/client.rs`, `src/local.rs`: SSH bootstrap, authenticated attachment, terminal/input lifetime and local clipboard acquisition.
- `src/terminal.rs`: authoritative terminal parser, every scrolled-row event, bounded replay, and native client scrollback rendering.
- `src/protocol.rs`: strict base64 byte fields, pre-authentication and authenticated frame bounds, terminal dimensions, launch and ticket types.
- `src/account.rs`: native passwd lookup and isolated cross-user dispatch.
- `src/history.rs`: bounded JSONL journal, locked readers/writers and atomic checkpoints.

The Python controller/workers, separate detach project, and Mosh remain the current operational stack; they are not silently replaced by this prototype.

## Tests asserting this spec

The [proof matrix](../wiki/systems/unified-pi-session.md#evidence-and-remaining-acceptance) records revision-specific fixture, native Pi and macOS results. This docs-only audit runs no tests. Current pending code needs fresh acceptance; earlier passes are not a current full-suite or CI claim.

- `src/server_tests.rs`: real PTY plus QUIC bulk/reconnect test retains all 80 markers exactly once and preserves the child; another test covers simultaneous clients, smallest-size changes and separate private image files. The child is an isolated scripted fixture, not real Pi login/paste acceptance.
- `src/terminal.rs` tests: bulk output at different physical sizes, ANSI/UTF-8 fragmentation, native scrollback, bounded expiry, replay, resize, soft wraps and query replies.
- `src/local.rs` tests: fragmented keyboard input, bracketed paste, raw terminal restoration and injectable clipboard-provider processes; earlier native macOS clipboard proof is recorded in the wiki.
- `src/config.rs`, `src/control.rs`, `src/pty.rs` tests: concrete configuration, detached lifecycle, unrelated-PID rejection and PTY size/output.
- Existing-stack regression `tests/test_mosh_resize.py` at `e34f683` remains RED through Mosh (80 expected markers, only 034–079 delivered at 165×46). Slow streaming passed. Neither observation is a repair of the old stack.

## Known gaps (current cycle)

- [ ] Prove actual Pi launch/resume, installed SSH/QUIC attachment and real network reconnect/address-change behavior; fixture reconnect is not full acceptance.
- [ ] Rerun native Pi image-editor, detach, resize and CLI acceptance on current code; earlier offline Pi passes do not prove full OAuth, credential handling or a model call.
- [ ] Prove simultaneous real Pi clients and macOS-to-Linux installed attachment. Earlier macOS native runtime passed; current CI remains pending.
- [ ] Verify current replay/reset/control/retry behavior and per-display geometry. The client expiry marker is implemented, not still protocol-only; see [retention](../wiki/systems/unified-pi-session.md#output-and-retention).
- [ ] Establish Pi VT capability coverage; alternate-screen applications and unknown VT callbacks are unsupported, not general terminal compatibility.
- [ ] Verify opt-in deployment and a user-approved migration approach that preserves conversations, processes and configuration; no operational Rust deployment has occurred. Existing legacy Pi processes are not authorized for migration or cancellation. Legacy detach has no SCM_RIGHTS handoff.

## Out of scope

- Claude and other agent backends: user selected Pi only.
- Mosh wire compatibility and predictive local echo: user selected replacement of both ends and no predictive echo.
- File `sync` and `fetch`: not selected for the current scope; this is not a permanent rejection.
- Disruptive replacement of the current stack before native acceptance and a safe migration decision.
- Restarting or cancelling existing sessions as a migration shortcut.

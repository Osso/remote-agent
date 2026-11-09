# Unified Rust Pi sessions

One Rust project provides Pi session management, detached PTY ownership and remote attachment. Source lives in `src/`. Rust is opt-in beside the unchanged legacy stack. Deployment and two-account attachment are proven at `fdaa0d6`, not full product acceptance. See the [implementation and proof matrix](../wiki/systems/unified-pi-session.md).

## What it must do

### Scope and lifecycle

- [x] Manage Pi sessions independently of attached clients, with Linux servers and Linux clients. Keep sessions alive across clean detach and attachment loss.
- [x] Support installed macOS-to-Linux attachment (arm64 macOS laptop at `7831e16`).
- [x] Start each session as an enabled systemd user service in the user's `agents.slice`; deploy its template and reload each server account's user manager.
- [x] Cancel by disabling and stopping the service, wait for the server PID to exit, then invoke the configured project cancellation hook with the conversation ID. Repeated stop must be a no-op so a failed hook can be retried. Preserve errors and retry state.
- [x] Support `remote-agent restart NAME` for the systemd session service on the selected host and user account. Resume the saved conversation. Restart interrupts foreground work and jobs in the service control group; do not promise to restore them.
- [x] Keep sessions manageable after binary upgrades by validating exact server argv and absolute state path, without requiring the current executable inode.
- [x] Restore the same conversation, session name, project and account after server loss and restart using the saved launch configuration. Do not promise restoration of running jobs, in-progress model turns or unsent editor text.
- [ ] Restore enabled sessions after an actual host reboot when the user manager starts with linger enabled. Simulated reboot and whole-unit kill/start proof do not establish actual reboot acceptance.
- [x] Keep account credentials, maps and existing legacy processes separate and unchanged during opt-in deployment and attachment.

### Output and transport

- [x] Retain bounded, configurable output only in server memory; report expired output when requested replay is no longer retained. Do not persist output history to disk.
- [x] Return `logs` only from a running server snapshot without joining the session or changing PTY dimensions.
- [x] Recover ordered output through tested QUIC outages and NAT port migration without duplicating reconnect-notice rows.
- [x] End attachment after five consecutive failed reconnect attempts; reset the count on any authenticated display frame. Use backoff ceilings of 500, 1000, 2000 and 4000 ms with 90–100% jitter.
- [x] Keep an attachment whose display queue fills: skip live frames for it until the queue drains, then send one snapshot with every skipped row, the latest input acknowledgement and any exit. Never disconnect it for falling behind; a reconnect replays full history and refills the queue.
- [x] Show reconnect, control, clipboard and server notices without scrolling terminal output; repaint over them on the next display, including a duplicate reconnect snapshot.
- [x] Make steady-state display cost proportional to changed rows: live updates carry a diff from the previous display and the client repaints only rows that differ from its viewport. Fresh attachment, replay and virtual resize carry the complete display; reset, resize and repaint clear the viewport.
- [x] Limit login frames to 8192 bytes before authentication; retain this limit by user decision.
- [x] Preserve typed input, PTY dimensions, clean detach and local terminal restoration within the tested native attachment boundaries.
- [x] When a reconnect ticket has a changed token after server restart, reset attachment input and display replay state, preserve PTY dimensions and retry failures, reconnect to the same conversation, and accept new input. Do not replay pending pre-restart input whose delivery is ambiguous. With the same token, preserve pending input replay across transient outages. Restart and pending-frame fixtures plus live input recovery are proven below; this does not upgrade an already-running client.
- [ ] Establish complete Pi VT capability coverage and per-display geometry. Alternate-screen applications and unknown VT callbacks remain unsupported.
- [ ] Prove simultaneous real Pi clients with shared PTY dimensions set to the smallest attached dimensions; fixtures alone are not full acceptance.
- [x] Grey (256-color 240 background, distinct from dark terminal backgrounds such as `#1f2430`) the physical cells outside a smaller shared PTY, and keep them grey after native history scrolls the viewport.

### Login and image paste

- [ ] Expose only `login PROVIDER` for Rust provider login, with `--host` and `--user` supported. For `claude`, execute the selected target user's `~/.local/bin/claude auth login --claudeai`; accept an executable SDK symlink and fail explicitly if it is missing. Retain an explicit `CLAUDE_CONFIG_DIR`, otherwise use the target home's native `.claude` default. Do not import Pi Anthropic grants, create or copy profiles, or change existing session credentials.
- [ ] Route other providers to `pi login PROVIDER`. Native Claude bridge credentials are distinct from Pi's Anthropic login store. Do not expose `pi-login` or `claude-account` as Rust aliases.
- [ ] Prove login routing, target host/user and environment, missing executable failure and unchanged credentials with controlled executables; these tests do not establish OAuth network success.
- [x] Prove current-revision real Pi image paste from the initiating client's local clipboard without a manual upload command, including simultaneous clients.
- [x] Preserve text paste framing, transfer images only on demand, and reject empty or failed clipboard reads without stale image insertion.

## How it works

- [Lifecycle, transport, rendering and evidence](../wiki/systems/unified-pi-session.md).
- [Cargo dependency inventory](../wiki/systems/unified-pi-session.md#cargo-dependency-inventory).
- [Account/project configuration](configuration.md#rust-account-and-launch-policy).
- [Legacy preservation constraints](session-backends.md).

## Implementation inventory

- `src/main.rs`, `src/config.rs`, `src/account.rs`, `src/control.rs`: commands, maps, target-account dispatch, launch records and service lifecycle.
- `remote-agent-rs@.service`, `deploy.py`, `deploy.sh`: user service template and opt-in installation.
- `src/server.rs`, `src/pty.rs`: QUIC server, in-memory output, snapshot logs and Pi PTY ownership.
- `src/client.rs`, `src/local.rs`: SSH control, attachment, reconnect, notices, terminal lifetime and clipboard acquisition.
- `src/terminal.rs`, `src/protocol.rs`: ordered display/replay, terminal parsing, authenticated frames and size limits.

## Tests asserting this spec

Main-thread proof supplied for `fdaa0d6`; this docs-only update ran no tests. Current-revision login routing tests and OAuth network success remain unverified.

- `tests/test_rust_attach.py`: 5/5, including simulated-reboot restore with native PTY/QUIC/Pi TUI components and fixture SSH/systemd.
- `tests/test_rust_network.py`: 3/3, including reconnect-notice regression; `tests/test_rust_retry_budget.py`: six outages on one client over 193 seconds.
- `tests/test_rust_upgrade.py`: sessions remain manageable after immutable binary replacement; `tests/test_rust_deploy.py`: 8/8 deployment cases.
- `server::tests::stalled_reader_is_caught_up_instead_of_disconnected`: a reader stalled for 3 s during 20000 output lines stays attached, observes a skipped sequence and receives each line exactly once plus its input acknowledgement; the pre-fix server resets the connection.
- Rust control/client/server unit tests: lifecycle, framing, reconnect and rendering behavior. `terminal::tests::one_changed_pi_row_*` and `scrolled_pi_chat_*` compare live diffs and row repaint at 165×46 against full snapshot repaints for identical screen and scrollback. Earlier terminal/local fixtures and native macOS results remain revision-scoped in the wiki.
- `terminal::tests::space_outside_a_smaller_pty_is_grey_after_attach_and_scroll`: every cell outside a 4×10 PTY on a 6×20 terminal is grey after attach and after two rows scroll into native history; cells inside keep the default background.
- `tests/test_rust_resize.py::test_physical_growth_with_second_client_pinning_pty_minimum`: a detached smaller client stops pinning the PTY within 2 seconds (RED before `0c6df6d`); the real agent-server paste run also asserts 40×120 with both clients and 46×165 after the smaller one detaches.
- Real agent-server paste acceptance at installed `c06678a` (`/var/tmp/pi-resize-fix/rust-production/accept-paste.py`, Linux Wayland client): two simultaneous clients each pressed Ctrl+V with a different private-clipboard PNG; the server saved both byte-identical and Pi's editor showed both paths on both clients; bracketed text containing Ctrl+V stayed text; an empty clipboard showed a notice and changed neither images nor editor.
- Real agent-server acceptance at `fdaa0d6`: both accounts' enabled/active units, launch identity/environment, real SSH/QUIC/Pi input and detach, whole-unit kill/start conversation restore, cancellation and legacy preservation. See the wiki for exact boundaries.

### Restart recovery — October 30, 2026

- Fix `443536a`, regression `eb3b4b6`: the isolated real CLI/QUIC/PTY restart test passed with the old server frozen and input queued. The new server did not execute that input; fresh input and display worked. `cargo check` and nine client tests passed.
- `./deploy.sh --rust` installed binary `4a3ff9449184957a60f480981adc12185f824bd2ff6ad813aac6ad29a41281bd` locally and for remote `osso`/`allison`. Maps and seven existing server PIDs remained unchanged. Fresh normal-TTY attach/detach restored terminal settings.
- The old user client was detached; its replacement, PID `413377`, then survived a separately authorized graphics restart of `wowdecomp` to server PID `3507504`. Conversation `e9f22590-027a-452a-bc36-a1312c9b5537` remained unchanged. Reversible arrow input moved the live selection down and back; no option was submitted. Evidence: `/tmp/remote-agent-restart-live-recovery.json`.
- The same-server outage test observed queued input execute once and preserved process lifetimes, but failed its display assertion. The existing history test duplicates `BULK_LINE_028`–`033` on both fixed and pre-fix binaries before reconnect. Ordered display replay is not established by this run. Logs: `/tmp/remote-agent-same-server-network.log` and `/tmp/remote-agent-history-baseline.log`.

## Known gaps (current cycle)

- [ ] Actual host reboot: not performed because it would stop legacy sessions.
- [ ] Full OAuth/model-call, macOS real-client image paste, per-display geometry and complete Pi VT acceptance remain open.

## Out of scope

- UUID-conflict handling; explicit `--resume UUID` semantics remain unchanged.
- Claude, Mosh wire compatibility and predictive local echo; Pi and replacement endpoints were selected.
- File `sync` and `fetch`; not selected for this scope.
- Migration or cancellation of existing legacy sessions, live PTY handoff, or disruptive replacement of the legacy stack.

# Unified Rust Pi sessions

One Rust project provides Pi session management, detached PTY ownership and remote attachment. Source lives in `src/`. Rust is opt-in beside the unchanged legacy stack. Deployment and two-account attachment are proven at `fdaa0d6`, not full product acceptance. See the [implementation and proof matrix](../wiki/systems/unified-pi-session.md).

## What it must do

### Scope and lifecycle

- [x] Manage Pi sessions independently of attached clients, with Linux servers and Linux clients. Keep sessions alive across clean detach and attachment loss.
- [x] Support installed macOS-to-Linux attachment (arm64 macOS laptop at `7831e16`).
- [x] Start each session as an enabled systemd user service in the user's `agents.slice`; deploy its template and reload each server account's user manager.
- [x] Cancel by disabling and stopping the service, wait for the server PID to exit, then invoke the configured project cancellation hook with the conversation ID. Repeated stop must be a no-op so a failed hook can be retried. Preserve errors and retry state.
- [x] Keep sessions manageable after binary upgrades by validating exact server argv and absolute state path, without requiring the current executable inode.
- [x] Restore the same conversation, session name, project and account after server loss and restart using the saved launch configuration. Do not promise restoration of running jobs, in-progress model turns or unsent editor text.
- [ ] Restore enabled sessions after an actual host reboot when the user manager starts with linger enabled. Simulated reboot and whole-unit kill/start proof do not establish actual reboot acceptance.
- [x] Keep account credentials, maps and existing legacy processes separate and unchanged during opt-in deployment and attachment.

### Output and transport

- [x] Retain bounded, configurable output only in server memory; report expired output when requested replay is no longer retained. Do not persist output history to disk.
- [x] Return `logs` only from a running server snapshot without joining the session or changing PTY dimensions.
- [x] Recover ordered output through tested QUIC outages and NAT port migration without duplicating reconnect-notice rows.
- [x] End attachment after five consecutive failed reconnect attempts; reset the count on any authenticated display frame. Use backoff ceilings of 500, 1000, 2000 and 4000 ms with 90–100% jitter.
- [x] Show reconnect, control, clipboard and server notices without scrolling terminal output; repaint over them on the next display, including a duplicate reconnect snapshot.
- [x] Make steady-state display cost proportional to changed rows: live updates carry a diff from the previous display and the client repaints only rows that differ from its viewport. Fresh attachment, replay and virtual resize carry the complete display; reset, resize and repaint clear the viewport.
- [x] Limit login frames to 8192 bytes before authentication; retain this limit by user decision.
- [x] Preserve typed input, PTY dimensions, clean detach and local terminal restoration within the tested native attachment boundaries.
- [ ] Establish complete Pi VT capability coverage and per-display geometry. Alternate-screen applications and unknown VT callbacks remain unsupported.
- [ ] Prove simultaneous real Pi clients with shared PTY dimensions set to the smallest attached dimensions; fixtures alone are not full acceptance.
- [x] Grey (256-color 240 background, distinct from dark terminal backgrounds such as `#1f2430`) the physical cells outside a smaller shared PTY, and keep them grey after native history scrolls the viewport.

### Login and image paste

- [ ] Retain full Pi provider login; invalid-provider CLI proof does not establish OAuth or credentials.
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

Main-thread proof supplied for `fdaa0d6`; this docs-only update ran no tests.

- `tests/test_rust_attach.py`: 5/5, including simulated-reboot restore with native PTY/QUIC/Pi TUI components and fixture SSH/systemd.
- `tests/test_rust_network.py`: 3/3, including reconnect-notice regression; `tests/test_rust_retry_budget.py`: six outages on one client over 193 seconds.
- `tests/test_rust_upgrade.py`: sessions remain manageable after immutable binary replacement; `tests/test_rust_deploy.py`: 8/8 deployment cases.
- Rust control/client/server unit tests: lifecycle, framing, reconnect and rendering behavior. `terminal::tests::one_changed_pi_row_*` and `scrolled_pi_chat_*` compare live diffs and row repaint at 165×46 against full snapshot repaints for identical screen and scrollback. Earlier terminal/local fixtures and native macOS results remain revision-scoped in the wiki.
- `terminal::tests::space_outside_a_smaller_pty_is_grey_after_attach_and_scroll`: every cell outside a 4×10 PTY on a 6×20 terminal is grey after attach and after two rows scroll into native history; cells inside keep the default background.
- `tests/test_rust_resize.py::test_physical_growth_with_second_client_pinning_pty_minimum`: a detached smaller client stops pinning the PTY within 2 seconds (RED before `0c6df6d`); the real agent-server paste run also asserts 40×120 with both clients and 46×165 after the smaller one detaches.
- Real agent-server paste acceptance at installed `c06678a` (`/var/tmp/pi-resize-fix/rust-production/accept-paste.py`, Linux Wayland client): two simultaneous clients each pressed Ctrl+V with a different private-clipboard PNG; the server saved both byte-identical and Pi's editor showed both paths on both clients; bracketed text containing Ctrl+V stayed text; an empty clipboard showed a notice and changed neither images nor editor.
- Real agent-server acceptance at `fdaa0d6`: both accounts' enabled/active units, launch identity/environment, real SSH/QUIC/Pi input and detach, whole-unit kill/start conversation restore, cancellation and legacy preservation. See the wiki for exact boundaries.

## Known gaps (current cycle)

- [ ] Actual host reboot: not performed because it would stop legacy sessions.
- [ ] Full OAuth/model-call, macOS real-client image paste, per-display geometry and complete Pi VT acceptance remain open.

## Out of scope

- UUID-conflict handling; explicit `--resume UUID` semantics remain unchanged.
- Claude, Mosh wire compatibility and predictive local echo; Pi and replacement endpoints were selected.
- File `sync` and `fetch`; not selected for this scope.
- Migration or cancellation of existing legacy sessions, live PTY handoff, or disruptive replacement of the legacy stack.

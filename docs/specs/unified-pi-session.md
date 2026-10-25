# Unified Rust Pi sessions — draft

A proposed single Rust project replaces the needed remote-agent, detach, and Mosh behavior for Pi sessions. This is a design draft, not an implemented replacement. Source for the replacement does not exist yet. The existing implementations remain in service. See [design notes](../wiki/systems/unified-pi-session.md).

## What it must do

### Confirmed scope

- [ ] Use one Rust project for session management, detached PTY ownership, and remote attachment.
- [ ] Support Pi only; replace both transport endpoints without Mosh wire compatibility.
- [ ] Do not provide predictive local echo.
- [ ] Support Linux and macOS clients, with Linux servers.
- [ ] Retain Pi provider login.
- [ ] Paste a local clipboard image into remote Pi without a separate manual `paste` command.
- [ ] Keep bounded, configurable output history; clearly report expired output when a requested replay position is no longer retained.

### Proposed session and transport acceptance

These details require user review; they are not a claim that every existing CLI feature must be retained.

- [ ] Keep Pi and its PTY alive when a client detaches or its network connection fails.
- [ ] Preserve all ordered output within the agreed retention boundary, including updates longer than the viewport; do not substitute the latest screen for output history.
- [ ] Reconnect after a network interruption or address change without losing or duplicating acknowledged terminal output.
- [ ] Preserve input, terminal dimensions, clean detach, and local terminal restoration.
- [ ] Retain the needed session commands and host/account/project routing, with user-specific credentials and configuration kept separate.

### Proposed image-paste acceptance

- [ ] The normal Pi image-paste action obtains the current local image without a separate upload command.
- [ ] Make image data available before Pi consumes the paste; do not paste a stale image after an empty clipboard or transfer failure.
- [ ] Preserve ordinary text paste and its terminal input framing.
- [ ] Transfer clipboard contents on demand, not by continuously synchronizing the clipboard.
- [ ] Associate the image with the intended attached session and account.

## How it works

- [Design notes and observed failure boundary](../wiki/systems/unified-pi-session.md).
- [Current account/project configuration](configuration.md).
- [Current session preservation constraints](session-backends.md).

## Implementation inventory

No unified Rust implementation exists. The current behavior is distributed across:

- `remote-agent.py`: local commands, SSH control requests, Mosh attachment, clipboard upload.
- `remote-agent-worker.py`: remote session lifecycle, provider login, clipboard helper.
- `remote-agent-user.py`: Unix-account routing.
- Separate Rust `detach` project: persistent PTY and local socket attachment.
- Upstream Mosh: network transport and screen-state synchronization.

## Tests asserting this spec

No passing tests establish the proposed replacement contract.

- `tests/test_mosh_resize.py::NativeMoshResizeTests.test_native_fixed_size_streaming_assistant_preserves_all_live_lines`: existing-stack diagnostic, slow incremental output passed; not replacement proof.
- The native bulk-output case in `tests/test_mosh_resize.py`, committed as `e34f683`, fails on the existing Mosh path: 80 numbered lines become only lines 034–079 at 165×46. Direct detach retains all 80. This is one required regression boundary, not the sole acceptance test.

## Known gaps (current cycle)

- [ ] Review session command scope and reconnect/replay behavior before implementation; specify retention units/default and how a fresh attachment obtains valid terminal state after history expires.
- [ ] Choose the Pi clipboard integration after inspecting the actual image-read path.
- [ ] Define acceptance for provider login and transparent image paste on supported platforms.
- [ ] Define a migration approach that preserves existing conversations, running processes, and configuration.

## Out of scope

- Claude and other agent backends: user selected Pi only.
- Mosh wire compatibility and predictive local echo: user selected replacement of both ends and no predictive echo.
- File `sync` and `fetch`: not selected in the first-version helper scope.
- Implementation or deployment during this design stage.
- Restarting or cancelling existing sessions as a migration shortcut.

# Unified Pi session design notes

Status: proposed, not implemented. The [specification](../../specs/unified-pi-session.md) owns requirements and open scope decisions.

## Observed loss boundary

The fixed-size native diagnostic uses an actual Pi assistant-message component, Rust detach, Mosh client/server, and a headless terminal at 165 columns × 46 rows. A bulk update contains 80 uniquely numbered lines.

| Path | Ordered markers received | Scrollback rows |
| --- | --- | --- |
| Pi → detach → terminal | 000–079 | 41 |
| Pi → detach → Mosh → terminal | 034–079 | 0 |

The missing prefix is already absent from the captured Mosh client output. This is not proof of a Pi widget row-count error. A separate slow incremental update retained all 80 lines, so output timing matters. The user's screenshot is cropped horizontally; its right edge does not establish clipping.

The reproduction is committed as `e34f683`; local diagnostic evidence is under `/var/tmp/pi-resize-fix/long-live-red/`, including `direct-vs-mosh.json`. No repair has been demonstrated.

## Mosh source boundary

Upstream source was inspected at `mosh-1.4.0`, commit `bc73a26316ede2a79259d859f8ee309b32412420`, cloned into `~/Repos/mosh`.

- `src/frontend/mosh-server.cc`, `serve()`, reads PTY output and calls `terminal.act()`.
- `src/statesync/completeterminal.cc`, `Complete::act()`, parses those bytes into terminal state; `Complete::diff_from()` computes screen differences.
- `src/frontend/stmclient.cc`, `STMClient::output_new_frame()`, renders received framebuffer state.
- `src/frontend/terminaloverlay.*` implements predictive local echo as well as separate connection-notification overlays.
- `src/network/network.cc`, `Connection::recv_one()`, handles a changed peer address. That behavior is distinct from durable output retention or restoring a dead server process.

Tokei counted 9,560 production C++/header code lines across 63 files, excluding comments, blanks, tests, fuzz targets, examples, and generated protobuf output. That is a source-size measurement, not a Rust implementation estimate.

## Proposed consolidation

One project can provide a local client and a persistent remote session process. They must have separate lifetimes even if they use the same binary. An ordered terminal-output stream would replace Mosh's framebuffer synchronization. With no predictive echo, the replacement does not need Mosh's prediction engine. Ordered byte delivery removes the need for framebuffer diffs as the output transport. It does not by itself establish that all terminal-state reconstruction can be removed.

This is not an implementation choice for a specific transport library. Authentication, replay positions, output retention, resize ordering, input delivery, and reconnection still require explicit design and behavioral tests. Transport address migration alone does not restore a session after its owning process exits.

The user selected bounded output history. When old output expires, an arbitrary retained byte tail is not a sufficient terminal-state checkpoint: escape sequences, cursor position, and active terminal modes can depend on preceding bytes. Fresh attachment and expired-offset recovery need a valid state baseline plus ordered retained output, or another demonstrated reconstruction method. This requirement may retain terminal parsing through an existing library; a complete removal of server terminal emulation is not yet justified.

## Current image-paste boundary

Pi's `packages/coding-agent/src/modes/interactive/interactive-mode.ts` maps the editor's image-paste action to `handleClipboardImagePaste()`, which invokes `readClipboardImage()`. On headless Linux, `packages/coding-agent/src/utils/clipboard-image.ts` reads image types and bytes through `wl-paste`.

The existing worker's `wl_paste()` helper serves a PNG previously uploaded by `remote-agent paste`. It does not fetch the local clipboard automatically. The new design must replace that manual staging step and establish image availability before Pi reads it. The exact integration remains open; it must not depend on timing a separate upload against forwarded keyboard input.

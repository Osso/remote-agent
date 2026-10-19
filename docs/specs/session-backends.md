# Managed session backends

The worker manages new sessions with dtach while retaining existing tmux sessions. Lifecycle requests are implemented in `remote-agent-worker.py`; attachment uses the controller's existing Mosh transport with `--no-init`. See [usage](../../README.md).

## What it must do

- [ ] New sessions explicitly record `backend: dtach`; missing dtach fails without creating session state or starting tmux instead.
- [ ] Existing records without a backend remain tmux sessions, including restart from their recorded conversation.
- [ ] Start preserves the selected harness, project, prompt, conversation and user environment. A configured resource scope uses the separate `remote-agent-NAME-dtach` unit, not the legacy session unit.
- [ ] Ctrl+backslash detaches without stopping the agent; reattachment uses `-r winch`, without injecting Ctrl+L. Restart resumes the recorded conversation without resending the initial prompt.
- [ ] List and status report dtach backend, attachment, liveness and recorded exit status from private runtime metadata and socket state. Locate finds saved records even after exit; list retains exited dtach records.
- [ ] Cancel terminates only the intended session and removes its private state; stale process identifiers must not target unrelated processes.
- [ ] Session state and sockets remain private to the owning account.
- [ ] Logs fail explicitly for dtach because terminal output is not retained or replayed; existing tmux pane history remains available through the legacy path.
- [ ] Mosh attachment retains `--no-init`; terminal-native scrollback is not a complete output-history guarantee.
- [ ] Deployment preserves existing user tmux configuration, including the deployed legacy copy targeted by an existing user symlink, and does not reload tmux configuration.

## How it works

- [Controller, worker and deployment usage](../../README.md).

## Implementation inventory

- `remote-agent-worker.py` — backend selection, lifecycle and harness launch.
- `remote-agent.py` — existing JSON requests and Mosh attachment.
- `deploy.py` — worker deployment preserving user terminal settings.

## Tests asserting this spec

- `tests/test_dtach.py` — real dtach process and PTY lifecycle, metadata, private state, explicit log errors, cancellation and failed startup. Source assertions alone do not establish PASS; new contract bullets remain unchecked until revision-scoped proof is available.
- `tests/test_remote_agent.py` — harness semantics, legacy sessions, controller and deployment fixtures.
- `tests/test_cross_user.py` — account boundaries and private state.

## Known gaps (current cycle)

- [ ] Final integrated backend verification and installed worker smoke pending.
- [ ] Real world-of-osso migration awaits production deployment and an idle, graceful conversation handoff.

## Out of scope

- Terminal history replay: dtach does not retain detached output or restore a previous terminal's scrollback.
- Transparent transfer of a running tmux process: an existing conversation must exit gracefully before resuming under dtach.
- Pi rendering changes: same-size reattachment may require a window resize for repaint.
- Complete Ghostty backlog: `--no-init` cannot replay output dtach never retained.
- Native cross-user dtach acceptance: existing identity-helper and live tmux evidence does not prove this backend's interactive account boundary; see [cross-user attachment](cross-user-attachment.md).

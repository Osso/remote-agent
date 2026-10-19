# Managed session backends

The worker manages new sessions with dtach while retaining existing tmux sessions. Lifecycle requests are implemented in `remote-agent-worker.py`; attachment uses the controller's existing Mosh transport with `--no-init`. See [usage](../../README.md).

## What it must do

- [ ] New sessions use dtach; missing dtach fails explicitly without starting tmux instead.
- [ ] Existing records without a backend remain tmux sessions, including restart from their recorded conversation.
- [ ] Start preserves the selected harness, project, prompt, conversation, user environment and resource scope.
- [ ] Detach and reattach preserve the running agent. Restart resumes its conversation without resending the initial prompt.
- [ ] List, locate and status identify managed sessions and report process termination truthfully.
- [ ] Cancel terminates only the intended session and removes its private state; stale process identifiers must not target unrelated processes.
- [ ] Session state and sockets remain private to the owning account.
- [ ] Logs explicitly report that dtach does not retain terminal output; existing tmux history remains available through the legacy path.
- [ ] Deployment preserves existing user tmux configurations and does not reload their servers.

## How it works

- [Controller, worker and deployment usage](../../README.md).

## Implementation inventory

- `remote-agent-worker.py` — backend selection, lifecycle and harness launch.
- `remote-agent.py` — existing JSON requests and Mosh attachment.
- `deploy.py` — worker deployment preserving user terminal settings.

## Tests asserting this spec

- `tests/test_dtach.py` — real dtach process and PTY lifecycle.
- `tests/test_remote_agent.py` — harness semantics, legacy sessions, controller and deployment fixtures.
- `tests/test_cross_user.py` — account boundaries and private state.

## Known gaps (current cycle)

- [ ] Final integrated backend verification and installed worker smoke pending.
- [ ] Real world-of-osso conversation handoff awaits completion of its production deployment.

## Out of scope

- Terminal history replay: dtach does not retain detached output or restore a previous terminal's scrollback.
- Transparent transfer of a running tmux process: an existing conversation must exit gracefully before resuming under dtach.
- Pi rendering changes: same-size reattachment may require a window resize for repaint.

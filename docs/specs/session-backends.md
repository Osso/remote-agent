# Managed session backends

The worker manages new sessions with dtach while retaining existing tmux sessions. Lifecycle requests are implemented in `remote-agent-worker.py`; attachment uses the controller's existing Mosh transport with `--no-init`. See [usage](../../README.md).

## What it must do

- [x] New sessions explicitly record `backend: dtach`; missing dtach fails without creating session state or starting tmux instead.
- [x] Existing records without a backend remain tmux sessions, including restart from their recorded conversation.
- [x] Start preserves the selected harness, project, prompt, conversation and user environment. A configured resource scope uses the separate `remote-agent-NAME-dtach` unit, not the legacy session unit.
- [x] Normal attachment reopens a saved conversation without manual path, environment or session repair. Repeated restarts reuse the same scope, preserving retained jobs and resource limits.
- [x] Active delegated scopes accept only the new launcher; older active scopes receive one fixed runtime delegation override in place. Inactive scopes remove only that owned override before recreating the same delegated unit. No per-reopen unit names or override files, cap changes or retained-job termination.
- [x] Startup readiness follows successful scope creation or attachment; scope failures surface explicitly.
- [x] Caller `COLORTERM` is forwarded on start and attach, persisted and refreshed before restart. Dtach launches set `TERM=xterm-256color`, advertising truecolor only for caller `truecolor`/`24bit`; no palette edit or running-process environment mutation.
- [x] Ctrl+backslash detaches without stopping the agent; reattachment uses `-r winch`, without injecting Ctrl+L. Restart resumes the recorded conversation without resending the initial prompt.
- [x] List and status report dtach backend, attachment, liveness and recorded exit status from private runtime metadata and socket state. Locate finds saved records even after exit; list retains exited dtach records.
- [x] Cancel terminates only the intended session and removes its private state; stale process identifiers must not target unrelated processes.
- [x] Session state and sockets remain private to the owning account.
- [x] Logs fail explicitly for dtach because terminal output is not retained or replayed; existing tmux pane history remains available through the legacy path.
- [x] The read-only `history` request exports a Pi session's saved active branch through the installed Pi CLI, with caller width and color capability. It does not parse private transcript files or modify the running agent. Unsupported harnesses and a confirmed live session without saved history return explicit unavailability; other export failures remain errors.
- [x] Mosh attachment retains `--no-init`; terminal-native scrollback is not a complete output-history guarantee.
- [x] Deployment preserves existing user tmux configuration, including the deployed legacy copy targeted by an existing user symlink. Asserted by `DeployTests.test_deploy_preserves_existing_tmux_configuration`; deployment commit `6c09830` has saved GREEN proof (2 tests).
- [ ] Deployment does not reload tmux configuration.

## How it works

- [Controller, worker and deployment usage](../../README.md).

## Implementation inventory

- `remote-agent-worker.py` — backend selection, lifecycle and harness launch.
- `remote-agent.py` — existing JSON requests and Mosh attachment.
- `deploy.py` — worker deployment preserving user terminal settings.

## Tests asserting this spec

- `tests/test_dtach.py` — real dtach process and PTY lifecycle, metadata, private state, explicit log errors, cancellation and failed startup. The 14-case lifecycle suite passed at `068e5c4`; deployed-worker checks also passed after fixture isolation in `46635a7`.
- `tests/test_dtach_scope.py` — real-systemd stable-scope reuse, retained jobs and limits, legacy delegation upgrade and startup failure boundaries: four cases passed at `dfeb004`.
- `tests/test_terminal_colors.py` — start/attach capability forwarding, normalization and account routing: four tests with 32 subcases passed at `232a98c`.
- `tests/test_remote_agent.py` — harness semantics, legacy sessions, controller and deployment fixtures.
- `tests/test_cross_user.py` — account boundaries and private state.
- `tests/test_history.py` — history export protocol, exact ANSI, unchanged state, input validation and explicit unavailability/error boundaries.

## Known gaps (current cycle)

- [ ] Full repository acceptance remains blocked by three unchanged periodic-runtime test cases (four subtests): expected audit deliveries are absent. Prior baseline reproduction is recorded at `/tmp/remote-agent-periodic-baseline-6cabad8-xbrl4sib`; this failure is outside the color/stable-scope fix. Backend, legacy, harness and cross-user fixture cases passed in the prior 139-test integration run; corrected controller and dependency-configured Node cases passed separately. Those earlier results do not verify the current scope changes.

## Out of scope

- Terminal history replay: dtach does not retain detached output or restore a previous terminal's scrollback.
- Transparent transfer of a running tmux process: an existing conversation must exit gracefully before resuming under dtach.
- Pi rendering changes: same-size reattachment may require a window resize for repaint.
- Complete Ghostty backlog: `--no-init` cannot replay output dtach never retained.
- Native cross-user dtach acceptance: existing identity-helper and live tmux evidence does not prove this backend's interactive account boundary; see [cross-user attachment](cross-user-attachment.md).

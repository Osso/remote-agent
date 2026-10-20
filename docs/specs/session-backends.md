# Managed session backends

The worker manages new sessions with dtach while retaining existing tmux sessions. Lifecycle requests are implemented in `remote-agent-worker.py`; attachment uses the controller's existing Mosh transport with `--no-init`. See [usage](../../README.md).

## What it must do

- [x] New sessions explicitly record `backend: dtach`; missing dtach fails without creating session state or starting tmux instead.
- [x] Existing records without a backend remain tmux sessions, including restart from their recorded conversation.
- [x] Start preserves the selected harness, project, prompt, conversation and user environment. With resource scoping enabled, dtach uses `remote-agent-NAME-dtach.scope`; legacy tmux retains `remote-agent-NAME.scope`.
- [x] Normal attachment reopens a saved conversation without manual path, environment or session repair. Both legacy tmux and dtach reuse their same stable scope across repeated restarts, preserving retained jobs, active-scope resource limits and the recorded backend.
- [x] Active delegated scopes accept only the new launcher; older active scopes receive one fixed runtime delegation override in place. Inactive scopes remove only that owned override before recreating the same delegated unit with configured limits. No per-reopen unit names or override files, active-scope cap changes or retained-job termination.
- [x] Dtach startup readiness follows successful scope creation or attachment; scope failures surface explicitly.
- [x] Caller `COLORTERM` is forwarded on start and attach, persisted and refreshed before restart. Dtach launches set `TERM=xterm-256color`, advertising truecolor only for caller `truecolor`/`24bit`; no palette edit or running-process environment mutation.
- [x] For dtach, Ctrl+backslash detaches without stopping the agent; reattachment uses `-r winch`, without injecting Ctrl+L. Restart resumes the recorded conversation without resending the initial prompt.
- [x] Each TTY dtach attachment enables bracketed paste for the managed Pi and Claude TUIs, including reattachment after detached startup. Multiline paste remains one paste without bare-CR submissions; non-TTY output receives no mode sequence. Detach preserves the running process.
- [x] List and status report dtach backend, attachment, liveness and recorded exit status from private runtime metadata and socket state. Locate finds saved records even after exit; list retains exited dtach records.
- [x] Cancel terminates only the intended session and removes its private state; stale process identifiers must not target unrelated processes.
- [x] Session state and sockets remain private to the owning account.
- [x] Logs fail explicitly for dtach because terminal output is not retained or replayed; existing tmux pane history remains available through the legacy path.
- [x] The read-only `history` request exports a Pi session's saved current, compaction-aware history through the installed Pi CLI, with caller width and color capability. It does not parse private transcript files or modify the running agent. Unsupported harnesses and a confirmed live session without saved history return explicit unavailability; other export failures remain errors.
- [x] Mosh attachment retains `--no-init`; terminal-native scrollback is not a complete output-history guarantee.
- [x] Before each TTY attach and retry, the controller prints the current Pi history locally over the reliable SSH control path and moves it above the live viewport. It preserves previous terminal scrollback and does not route the snapshot through Mosh. Explicit unavailability permits attachment with a notice; export errors stop it.
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
- `tests/test_dtach.py::DtachTests.test_bracketed_multiline_paste_on_first_and_repeat_attach` — real dtach/PTY attachments to a fixture TUI that enables paste once before attachment: terminal enable sequence, exact multiline paste content, no submissions, retained process and clean detach on first and repeat attachment. `test_non_tty_attach_does_not_emit_bracketed_paste_mode` covers real pipe output without the mode sequence. Local transport proof only; not SSH/Mosh or actual-harness acceptance.
- `tests/test_dtach_scope.py` — real-systemd stable-scope reuse, retained jobs and limits, legacy delegation upgrade and startup failure boundaries. Legacy tmux regression exercises main exit with a retained detached child, ordinary attach twice with the selected saved conversation, then inactive same-name recreation.
- `tests/test_terminal_colors.py` — start/attach capability forwarding, normalization and account routing: four tests with 32 subcases passed at `232a98c`.
- `tests/test_remote_agent.py` — harness semantics, legacy sessions, controller and deployment fixtures.
- `tests/test_cross_user.py` — account boundaries and private state.
- `tests/test_history.py` — history export protocol, exact ANSI, unchanged state, input validation and explicit unavailability/error boundaries; nine passing tests reported for the current worker.
- `tests/test_history_attach.py` and `tests/history_scrollback_fixture.cjs` — four real-PTY controller cases, including headless normal-buffer assertions for all 200 ordered history markers above the live viewport, retry restoration and unavailable/error behavior. SSH and Mosh are fixtures in these regression tests.

On 2026-10-20, installed SSH/Mosh/dtach/Pi acceptance exercised first attachment and reattachment for both the default account and `--user allison`. Each TTY received bracketed-paste enablement, displayed both pasted lines without submitting conversation history, and detached cleanly with the agent alive. Disposable sessions were removed; existing session inventories were unchanged. Local real-dtach fixtures additionally assert exact paste contents and zero submissions. This does not claim native desktop clipboard-dialog or Claude acceptance.

Separate installed acceptance captured two real SSH/Mosh/dtach/Pi attachments at 61×282: all 3,600 current-history rows matched exactly in the normal terminal buffer, above the live viewport. Independent replay confirmed both captures; this is terminal-buffer proof, not a Ghostty screenshot or proof of live-view repaint.

## Known gaps (current cycle)

- [ ] Same-size dtach attachment can leave the live viewport blank until Pi repaints. Current-history scrollback is restored independently; viewport repaint is not fixed here.
- [ ] A prior full repository run recorded three periodic-runtime case failures (four subtests), reproduced on pre-change `6cabad8` under the same installed runtime. Scoped history tests pass; no whole-repository pass is claimed.

## Out of scope

- Raw terminal byte replay, pre-compaction archive restoration, images and expanded tool output: the snapshot contains Pi's current saved context with normal collapsed output, not captured PTY output.
- Transparent transfer of a running tmux process: an existing conversation must exit gracefully before resuming under dtach.
- Pi rendering changes: same-size reattachment may require a window resize for repaint.
- Complete Ghostty backlog: `--no-init` cannot replay output dtach never retained.
- Exhaustive cross-user dtach isolation matrix and full-history pixel proof: one user-authorized SSH/Mosh/dtach Pi attach/detach at 48×200 returned exit 0, with target cwd and selected conversation confirmed through process and health metadata and the existing tmux client preserved. Earlier disposable tmux isolation evidence remains separate; see [cross-user attachment](cross-user-attachment.md#tests-asserting-this-spec).

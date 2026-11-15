# Operational gotchas

- `./deploy.sh` installs the client locally and the server on every configured account (`deploy_users`); target users' maps are preserved. Running session servers keep their old immutable binary until restarted. See [configuration](docs/specs/configuration.md).
- Before attaching, restarting or cancelling, reconcile Unix account, project cwd, recorded conversation, saved Pi metadata and live process identity. Duplicate conversation IDs in different projects are not equivalent; resolve ambiguous selection with the user. Never cancel a session as a migration shortcut.
- The Python worker was removed on 2026-11-14, but agent-server still has `remote-agent-*-dtach.scope` units holding live processes and a `~/.local/bin/wl-paste` link into the old worker checkout. Do not stop or clean them without the user.
- Verify the exact normal command in a real TTY, including `--user`. Check intended account/project/conversation, clean Ctrl+backslash detach and preservation of existing processes, not merely exit 0.
- An unsupported terminal sequence poisons the server's terminal state and `__serve` exits, which kills the session's Pi. When Pi emits a new sequence, add it to `src/terminal.rs` with a test.

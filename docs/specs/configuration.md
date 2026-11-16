# Per-user configuration

Host and project mappings belong to the current user's `~/.config/remote-agent/`, independent of the code checkout. Mapping consumers are the Rust client/server (`src/config.rs`) and `deploy.py`. Pi's bundled periodic-checks extension discovers projects independently of these maps; see the [canonical contract](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md). See [mapping setup and deployment](../../README.md#mapping-setup).

## What it must do

- [x] Read `remote-hosts.json` and `remote-projects.json` from `~/.config/remote-agent/`; missing user maps fail explicitly without consulting code-directory copies. Asserted by `config::tests::missing_maps_and_invalid_explicit_pi_fail`.
- [x] Optional project `pi_executable` selects an absolute host executable path for Pi. A configured value must name an existing executable regular file; invalid values fail explicitly without PATH fallback. Absence retains default resolution. Asserted by `config::tests::resolves_project_without_changing_map_bytes` and `missing_maps_and_invalid_explicit_pi_fail`.
- [x] Optional project `pi_cancel_executable` uses the same validation. Cancellation invokes `[path, conversation_id]` without a shell after the session server exits, including already-dead sessions. Nonzero hook exit fails explicitly and preserves state for retry; restart never invokes the hook. Container termination remains the hook's responsibility. Asserted by `config::tests::cancel_hook_*` and control tests.
- [x] Deployment sends local map bytes to the default SSH account's `~/.config/remote-agent/` with directory mode `0700` and file modes `0600`. Asserted by `tests/test_deploy.py`.
- [x] Optional host `deploy_users` lists existing Unix usernames installed after the default SSH account, in configured order, through existing `authsudo` authorization with an isolated account environment. No account discovery. Their existing maps remain byte-for-byte unchanged; missing maps fail without copying operator maps. Wrapped/Windows hosts are rejected. No service/session restart. Asserted by `tests/test_deploy.py`; real UID/authsudo acceptance is a live verification responsibility.
- [ ] Deployment does not install a user-level `periodic-checks.ts` extension or distribute project checklists. Pi owns bundled extension installation, scheduling, and audit policy; see the [canonical contract](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md) and [host-private opt-in instructions](../../README.md#optional-periodic-checks). No checklist means no timer or audit.

## Rust account and launch policy

Main-thread proof at `fdaa0d6` includes real agent-server acceptance for `osso`
and `allison`; actual reboot and current macOS CI remain unproven.

- [x] Resolve `projects` and session operations through RPC to the selected host/account, using that account's private maps.
- [x] Native Linux `--user` must resolve identity/home through passwd, enter the target home, and execute only the target Rust RPC/login operation through existing `authsudo` authorization. Start with an empty environment plus the 13 explicit account variables; do not inherit administrator credentials.
- [x] Require selected-host `cargo_build_jobs` as a positive integer string and pass it as `CARGO_BUILD_JOBS`. Pass optional `build_host_scripts` as `BUILD_HOST_SCRIPTS` without an implicit path. Record launch values explicitly in the launch record's environment field.
- [x] Run each session as an enabled systemd user service in the selected account's user `agents.slice`. Install the service template, the bundled slice when none exists, and reload every server account's user manager during deployment.
- [x] Identify the server by exact `__serve --state <absolute state path>` argv, without requiring the current installed executable inode, so upgrades preserve manageability.
- [x] Cancellation must disable and stop the session unit, then wait for the server PID to exit before invoking `pi_cancel_executable` with the conversation ID. Repeated unit stop is a no-op so a failed hook can be retried. Keep errors and saved state available; never run the hook after an identity mismatch or while the server remains alive.
- [ ] Restore enabled sessions when the user manager starts after an actual reboot, with linger required. Whole-unit kill/start and simulated-reboot tests establish conversation restoration, not actual host reboot acceptance.

Deployment installs immutable private hash-addressed versions and updates the
stable link with compare-and-swap protection. Fixture installation tests and real
two-account deployment/attachment at `fdaa0d6` are separate proof boundaries.
Linger state and service mechanics are recorded in the
[implementation and proof matrix](../wiki/systems/unified-pi-session.md).

## How it works

- [Mapping setup and deployment](../../README.md#mapping-setup).
- [Optional periodic checks](../../README.md#optional-periodic-checks).

## Implementation inventory

- `deploy.py` — private mapping transfer and Rust installation.
- `src/account.rs`, `src/config.rs`, `src/main.rs` — native target-account dispatch, map selection and launch environment.
- `src/control.rs` — launch records, server identity validation and user service lifecycle.
- `remote-agent@.service`, `agents.slice` — per-session user service template and its slice.

## Tests asserting this spec

- `tests/test_deploy.py` — real local subprocesses with fixture SSH/rustc/cargo/systemctl/authsudo: map transfer, preserved target maps, slice and unit installation, immutable versions, platform routing and failure handling. No real UID switch or live deployment.
- `tests/test_periodic_runtime.py` with `tests/fixtures/periodic-runtime-provider.ts` — installed-Pi integration acceptance for bundled periodic commands, without explicit `--extension` injection. Uses private state and an offline faux provider during held model/tool turns.

Native scheduler/dispatch coverage belongs to [Pi's scheduler spec](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md#tests-asserting-this-spec); this spec makes no bundled-runtime verification claim.

Rust behavior is covered by fixtures in `src/account.rs`, `src/config.rs`,
`src/control.rs`, `src/main.rs`, `tests/test_attach.py` and
`tests/test_deploy.py`, plus native network, retry-budget and upgrade tests.
Main-thread proof includes real two-account SSH/QUIC/Pi acceptance at `fdaa0d6`.
Actual reboot was not performed. GitHub run `40226091616` passed the Ubuntu and
macOS jobs at `fdaa0d6`. See the wiki proof matrix for remaining gaps.

## Known gaps (current cycle)

- [ ] On 2026-11-14 the native attach, network, resize and retry-budget tests had 5 failures, the same at `93244a4` before the Python removal.

## Out of scope

- Project-specific checklists and unrelated refactors.
- Repository history rewriting; removed personal maps remain in existing history.

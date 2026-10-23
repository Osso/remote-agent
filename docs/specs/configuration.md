# Per-user configuration

Host and project mappings belong to the current user's `~/.config/remote-agent/`, independent of the code checkout. Mapping consumers live in `remote-agent.py`, `remote-agent-worker.py`, and `deploy.py`. Pi's bundled periodic-checks extension discovers projects independently of these maps; see the [canonical contract](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md). See [mapping setup and deployment](../../README.md#mapping-setup).

## What it must do

- [x] Controller and worker read `remote-hosts.json` and `remote-projects.json` from `~/.config/remote-agent/`; missing user maps fail explicitly without consulting code-directory copies. Asserted by `ConfigurationTests`.
- [x] Project listing, sync, fetch, launch, and clipboard operations work with isolated user mappings and explicit/default host selection. Asserted by targeted `ControllerTests`, `SyncTests`, and `WorkerTests`.
- [x] Optional project `pi_executable` selects an absolute host executable path only for Pi. A configured value must be a nonempty string naming an existing executable regular file; invalid values fail explicitly without PATH fallback. Absence retains default resolution, and Claude ignores the setting. New launches, explicit resumes, saved-conversation restarts, and terminal-history exports resolve the current project setting without changing cwd, PATH, or environment. Asserted by real-tmux `ProjectPiExecutableTests` and subprocess `WorkerHistoryTests`; container forwarding and live dtach attachment are not established by these fixtures.
- [x] Optional project `pi_cancel_executable` uses the same absolute existing executable regular-file validation as `pi_executable`. Cancellation of recorded Pi sessions invokes `[path, conversation_id]` without a shell after host tmux/dtach shutdown and before deleting saved state, including already-dead host sessions. The recorded conversation is required and follows request resume-ID validation. Nonzero hook exit fails explicitly and preserves state for retry; no fallback. Dtach identity mismatch prevents invocation, and restart never invokes the hook. Absent configuration and Claude cancellation remain unchanged. Behavioral fixtures in `ProjectPiExecutableTests` and `DtachTests` cover selected conversation, shutdown ordering, failure/retry, validation, restart and identity refusal. Container termination itself remains the hook's responsibility; no Docker acceptance is claimed.
- [x] Deployment sends local user mapping bytes to the selected remote user's `~/.config/remote-agent/`, with directory mode `0700` and file modes `0600`, including replacement of existing public permissions. Fresh deployed code directories contain neither map. Asserted by `DeployTests`.
- [x] Optional host `deploy_users` lists existing Unix usernames for deployment after the default SSH account, in configured order, through existing `authsudo` authorization. No account discovery. Homes resolve through the remote account directory; each target uses an isolated account environment. The same code payload/revision is deployed while both existing target maps remain byte-for-byte unchanged; missing maps or failed user deployments fail explicitly without copying operator maps. Wrapped/Windows cross-user deployments are rejected; absent/empty lists retain default deployment behavior. No service/session restart or saved-conversation modification. Asserted by subprocess fixtures in `tests/test_deploy_users.py`; real UID/authsudo acceptance remains a live verification responsibility.
- [x] Optional selected-host `build_host_scripts` is passed unchanged as `BUILD_HOST_SCRIPTS` to launched and restarted agents through tmux. An absent setting injects no value or personal fallback; an inherited caller value remains unchanged. Asserted by `WorkerTests.test_build_scripts_*`.
- [ ] Deployment does not install a user-level `periodic-checks.ts` extension or distribute project checklists. Pi owns bundled extension installation, scheduling, and audit policy; see the [canonical contract](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md) and [host-private opt-in instructions](../../README.md#optional-periodic-checks). No checklist means no timer or audit.

## Shared Claude credential ownership

- [x] Named accounts use only `~/.config/pi-claude-bridge/profiles/NAME/.credentials.json`; listing reads canonical directories. Native Claude launches use the selected canonical `CLAUDE_CONFIG_DIR` and remove inherited API/OAuth token overrides. Pi keeps caller credential environment. New launches require a selected owned account with a nonempty OAuth access token; no global fallback. Existing sessions remain untouched.
- [x] Account selection changes only `~/.claude/.active-profile`. Switching never copies or writes back grants. No worker action clears a selection, so a retained legacy global grant cannot become the native launch credential. Deleted/empty canonical credentials remain cleared regardless of legacy global/named credentials.
- [x] Bridge prepares legacy profiles before activation and owns migration. Workers require `.credential-owner` containing `shared-v1\n`; initialization takes an exclusive `.credential-migration` directory lock and never removes a preexisting lock. Explicit named login creates or claims its canonical directory with mode `0700` and marker, including preexisting unmarked directories with missing/empty/invalid credentials; it preserves canonical bytes until native login and never imports legacy grants. Invalid existing ownership markers and symlink profile directories fail explicitly; existing owned directories permit reauthentication.
- [x] `claude-account --login NAME` validates/prepares through the JSON worker, runs SSH worker `--claude-login NAME` directly in canonical config, and selects NAME only after successful authentication and nonempty token validation. Failure preserves old selection and other account grants. No global staging/release step.
- [x] Explicit `--name-current NAME` imports only an unnamed global login with a nonempty OAuth access token, moving it once into a previously absent private canonical directory with marker; existing/cleared accounts cannot be overwritten. See [shared accounts](../../README.md#shared-claude-accounts). Asserted by local subprocess fixtures in `tests/test_claude_account_freshness.py` and controller protocol tests; no real authentication, migration/deploy, or live harness proof is claimed.

## How it works

- [Mapping setup and deployment](../../README.md#mapping-setup).
- [Optional periodic checks](../../README.md#optional-periodic-checks).

## Implementation inventory

- `remote-agent.py` — controller host and project mapping consumption.
- `remote-agent-worker.py` — worker mapping consumption and selected-host agent environment.
- `deploy.py` — separate code archive and private mapping transfer.

## Tests asserting this spec

- `tests/test_deploy_users.py` — real local subprocesses with fixture SSH/authsudo/account lookup: stale target worker upgrade followed by saved-history export, isolated target environment, unchanged maps and saved state, default-only behavior, missing-map/account/authorization failures, and unsupported transport rejection. No real UID switch or live deployment.

- `tests/test_remote_agent.py` — isolated configuration, selected controller/sync/worker behavior, real-tmux host environment propagation, and local execution of deployed payload.
- `tests/test_periodic_runtime.py` with `tests/fixtures/periodic-runtime-provider.ts` — installed-Pi integration acceptance for bundled periodic commands, without explicit `--extension` injection. Uses private state and an offline faux provider during held model/tool turns. Passing proof against the new bundle belongs to the implementation gate.

Native scheduler/dispatch coverage belongs to [Pi's scheduler spec](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md#tests-asserting-this-spec); this spec makes no bundled-runtime verification claim.

## Known gaps (current cycle)

- [ ] Desktop WSL is inactive; migrate its existing code-directory maps when that host is used again.

## Out of scope

- Project-specific checklists and unrelated refactors.
- Repository history rewriting; removed personal maps remain in existing history.

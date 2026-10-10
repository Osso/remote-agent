# Per-user configuration

Host and project mappings belong to the current user's `~/.config/remote-agent/`, independent of the code checkout. Mapping consumers live in `remote-agent.py`, `remote-agent-worker.py`, and `deploy.py`. The generic `periodic-checks.ts` extension discovers projects independently of these maps. See [mapping setup and deployment](../../README.md#mapping-setup).

## What it must do

- [x] Controller and worker read `remote-hosts.json` and `remote-projects.json` from `~/.config/remote-agent/`; missing user maps fail explicitly without consulting code-directory copies. Asserted by `ConfigurationTests`.
- [x] Project listing, sync, fetch, launch, and clipboard operations work with isolated user mappings and explicit/default host selection. Asserted by targeted `ControllerTests`, `SyncTests`, and `WorkerTests`.
- [x] Deployment sends local user mapping bytes to the selected remote user's `~/.config/remote-agent/`, with directory mode `0700` and file modes `0600`, including replacement of existing public permissions. Fresh deployed code directories contain neither map. Asserted by `DeployTests`.
- [x] Optional selected-host `build_host_scripts` is passed unchanged as `BUILD_HOST_SCRIPTS` to launched and restarted agents through tmux. An absent setting injects no value or personal fallback; an inherited caller value remains unchanged. Asserted by `WorkerTests.test_build_scripts_*`.
- [x] Deployment installs the generic periodic extension at user level without bundling project checklists. Worker launch and restart add no explicit extension arguments. Asserted by `DeployTests` and `WorkerTests.test_user_extension_*`.
- [x] Periodic checks opt in through `periodic-checks.md` at the current Git project root, without host or project mappings. Asserted by scheduler runtime tests.

## How it works

- [Mapping setup and deployment](../../README.md#mapping-setup).
- [Optional periodic checks](../../README.md#optional-periodic-checks).

## Implementation inventory

- `remote-agent.py` — controller host and project mapping consumption.
- `remote-agent-worker.py` — worker mapping consumption and selected-host agent environment.
- `deploy.py` — separate code archive and private mapping transfer.
- `periodic-checks.ts` — Git-root checklist discovery independent of mapping configuration.

## Tests asserting this spec

- `tests/test_remote_agent.py` — isolated configuration, selected controller/sync/worker behavior, real-tmux host environment propagation, and local execution of deployed payload.
- `tests/test_periodic_scheduler.mjs` — isolated extension scheduling, Git-root/checklist discovery, and unmapped-project participation; also executed from deployed code.

## Known gaps (current cycle)

- [ ] Desktop WSL is inactive; migrate its existing code-directory maps when that host is used again.

## Out of scope

- Project-specific checklists and unrelated refactors.
- Repository history rewriting; removed personal maps remain in existing history.

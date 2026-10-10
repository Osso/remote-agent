# Per-user configuration

Host and project mappings belong to the current user's `~/.config/remote-agent/`, independent of the code checkout. Mapping consumers live in `remote-agent.py`, `remote-agent-worker.py`, and `deploy.py`. The generic `periodic-checks.ts` extension discovers projects independently of these maps. See [mapping setup and deployment](../../README.md#mapping-setup).

## What it must do

- [x] Controller and worker read `remote-hosts.json` and `remote-projects.json` from `~/.config/remote-agent/`; missing user maps fail explicitly without consulting code-directory copies. Asserted by `ConfigurationTests`.
- [x] Project listing, sync, fetch, launch, and clipboard operations work with isolated user mappings and explicit/default host selection. Asserted by targeted `ControllerTests`, `SyncTests`, and `WorkerTests`.
- [x] Deployment sends local user mapping bytes to the selected remote user's `~/.config/remote-agent/`, with directory mode `0700` and file modes `0600`, including replacement of existing public permissions. Fresh deployed code directories contain neither map. Asserted by `DeployTests`.
- [x] Optional selected-host `build_host_scripts` is passed unchanged as `BUILD_HOST_SCRIPTS` to launched and restarted agents through tmux. An absent setting injects no value or personal fallback; an inherited caller value remains unchanged. Asserted by `WorkerTests.test_build_scripts_*`.
- [x] Deployment installs the generic periodic extension at user level without bundling project checklists. Worker launch and restart add no explicit extension arguments. Asserted by `DeployTests` and `WorkerTests.test_user_extension_*`.
- [x] Periodic checks opt in through ignored, host-private `periodic-checks.md` at the current Git project root, without host or project mappings. Each host maintains its own file; deployment never copies it. An absent file means no timer. Asserted by scheduler runtime and deployment tests.
- [x] Google OKF YAML frontmatter requires a nonempty string `type` and custom positive safe-integer `interval_seconds` within 1–2147483 seconds; unknown fields are allowed. Missing, malformed, or invalid metadata raises an explicit error and disables the timer, with no fixed 30-minute fallback. Asserted by scheduler metadata tests.
- [x] Metadata is read on `session_start`/reload; the first tick waits the full configured interval. Metadata edits require another start/reload; ticks do not reread metadata. Asserted by scheduler runtime tests.

- [x] `/periodic-checks status` (and no-argument `/periodic-checks`) reports `armed` or `disabled`, the checklist path when known, active `interval_seconds` and next scheduled tick as an ISO timestamp when armed, or the disabled reason otherwise. Other arguments produce a usage warning. Status reads current scheduler state without triggering an audit, resetting the timer, or applying metadata edits. Asserted by scheduler command tests.
- [x] Existing Pi sessions require reload (or a new session) to discover the newly installed command. A next scheduled tick does not guarantee an audit: busy sessions, pending messages, and audit overlap skip dispatch. Asserted by scheduler runtime tests for skipped ticks; command discovery requires extension loading.

## How it works

- [Mapping setup and deployment](../../README.md#mapping-setup).
- [Optional periodic checks](../../README.md#optional-periodic-checks).

## Implementation inventory

- `remote-agent.py` — controller host and project mapping consumption.
- `remote-agent-worker.py` — worker mapping consumption and selected-host agent environment.
- `deploy.py` — separate code archive and private mapping transfer.
- `periodic-checks.ts` — Git-root checklist discovery, bounded metadata scheduling, and read-only scheduler status, using Pi's exported YAML `parseFrontmatter` without a new dependency.

## Tests asserting this spec

- `tests/test_remote_agent.py` — isolated configuration, selected controller/sync/worker behavior, real-tmux host environment propagation, and local execution of deployed payload.
- `tests/test_periodic_scheduler.mjs` — isolated extension scheduling, OKF metadata validation, full-interval timing, start/reload metadata changes, Git-root/checklist discovery, unmapped-project participation, read-only status output and argument validation, and skipped ticks while busy or overlapping; also exercised from deployed code. Native Node execution requires the installed Pi npm package to be resolvable, including through `NODE_PATH` for compiled-only Pi installations. See the [canonical test command and prerequisites](../../README.md#tests).

## Known gaps (current cycle)

- [ ] Desktop WSL is inactive; migrate its existing code-directory maps when that host is used again.

## Out of scope

- Project-specific checklists and unrelated refactors.
- Repository history rewriting; removed personal maps remain in existing history.

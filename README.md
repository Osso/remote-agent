# remote-agent

Controller and remote worker for explicitly requested Pi or Claude terminal
sessions. Sessions run in tmux under a systemd resource scope; commands inspect,
attach to, or cancel sessions on one selected host. This is not a turnkey host
provisioner or a cross-host session discovery service.

## Prerequisites

- Controller: Python 3, OpenSSH client, Git, rsync, and Mosh for attachment.
- Remote environment: Python 3, SSH access, Git, rsync, tmux, mosh-server,
  systemd, and a usable user manager. Configure linger if sessions must survive
  logout, and authorize system scopes if using a system-level `agents.slice`.
- Install and authenticate the chosen harness independently on the remote host:
  `pi` for Pi (the default), or the `i` wrapper for Claude. These executables and
  their configuration are user-provisioned, not installed by deploy.
- Create project checkouts at their mapped remote paths. Provision project build
  tools, dependencies, permissions, and resource limits separately.

## Mapping setup

Create `~/.config/remote-agent/` with mode `0700`. Store both files below with
mode `0600`. Examples are fictional; replace destinations and paths for your
own controller and remote user.

`~/.config/remote-agent/remote-hosts.json`:

```json
{
  "agent-server": {
    "ssh": "developer@build.example.invalid",
    "home": "/home/developer",
    "wrap": [],
    "windows_shell": false,
    "cargo_build_jobs": "4"
  }
}
```

`~/.config/remote-agent/remote-projects.json`:

```json
{
  "sample": {
    "local": "/home/developer/Projects/sample",
    "remote": "Projects/sample"
  }
}
```

`local` is an absolute controller path; `remote` is relative to the remote
user's home. Mappings are per-user configuration, not repository files. Missing
maps fail explicitly; code-directory copies are not consulted.

Host-targeting commands default to the `agent-server` key. Pass `--host HOST`
to select another configured key; no automatic rerouting or aggregate listing
occurs. `remote-agent projects` displays the local project map, not remote
checkout availability. An SSH destination may be an alias configured separately
in SSH configuration. `REMOTE_AGENT_<HOST>_SSH` overrides that host's destination
(uppercase host key, with hyphens replaced by underscores).

Optional host `build_host_scripts` is passed unchanged as `BUILD_HOST_SCRIPTS`
to agent processes. Set an absolute remote path only when your projects require
it; no project-specific path is injected otherwise.

Native Linux uses an empty `wrap` and `windows_shell: false`. Windows/WSL
transport requires user-specific command wrapping, SSH shell configuration,
Linux user/home selection, systemd availability, and VM lifetime settings.
Deployment does not provision these. UNC fetch paths require the explicitly
selected `desktop` host; native Linux paths do not use WSL translation.

## Session commands

After deployment, ensure `~/.local/bin` is in the controller's `PATH`. Before
that, invoke `python3 remote-agent.py` from this checkout instead of
`remote-agent`.

```text
remote-agent projects
remote-agent start sample demo --agent-harness pi
remote-agent start sample task --prompt-file ./prompt.txt
remote-agent start sample continued --resume CONVERSATION_ID
remote-agent list
remote-agent status demo
remote-agent logs demo
remote-agent attach demo
remote-agent cancel demo
```

`start PROJECT [NAME]` defaults the session name to the project. Select Claude
with `--agent-harness claude`. Names use 1–64 letters, digits, underscores, or
hyphens, starting with a letter or digit. Use the same `--host HOST` on subsequent
commands when starting on a non-default host. Session inspection includes saved
records when tmux sessions no longer exist. Cancel terminates the selected
session; it is not a project cleanup command. Terminal logs may contain sensitive
content; review them before sharing.

Control requests use SSH. Interactive attachment uses Mosh with SSH bootstrap,
requires reachable Mosh UDP ports, and has no SSH fallback. Attachment retries
dropped connections; the worker can resume a recorded conversation after its
session is lost. This does not guarantee recovery of unsaved work or availability
of the host, harness, or provider.

## File transfer

```text
remote-agent sync sample --dry-run
remote-agent sync sample
remote-agent sync sample --pull
remote-agent fetch output/report.txt --project sample
remote-agent fetch '~/artifacts/chart.png' --open
```

Sync transfers the current branch's commits through Git and copies modified and
untracked, non-ignored working files through rsync; it does not copy `.git` or
delete receiving-side files. Pull reverses transfer direction. Git operations
require compatible checkouts and fast-forwardable history; sync is not a conflict
resolver or checkout provisioner.

Fetch accepts remote absolute paths, home-relative paths, and project-relative
paths. `--project` selects a path mapping, not a host. Without it, relative-path
discovery searches mapped checkouts on the selected host only. Files and folders
land in `~/Downloads/remote-agent/`; folders retain their name and update
incrementally. `--open` opens fetched images through Chromium and requires that
controller-side integration.

## Deploy

```text
./deploy.sh
./deploy.sh --host agent-server
```

`deploy.sh` deploys remotely first, then links the local controller and installs
its Pi extension. Local-only users should not run it blindly; use the local
helper below instead.

Deploy installs its selected payload under the remote user's
`~/Projects/cli/remote-agent/`, records the controller revision under
`~/data/remote-agents/`, and links the controller's
`~/.local/bin/remote-agent` to this checkout. It copies both local mapping files
byte-for-byte into the remote user's private configuration directory, enforcing
`0700`/`0600` permissions. All entries in those files are copied, not just the
selected host. Maps are excluded from the code archive; old code-directory maps
are not removed automatically.

Deployment links remote `~/.tmux.conf` and `~/.local/bin/wl-paste` to shipped
files and refuses to replace unmanaged files or foreign symlinks at those paths.
It preserves an existing system or user `agents.slice`; otherwise it installs
the bundled user slice and reloads the user manager. Review its limits for your
host. System-scope authorization, SSH configuration, Mosh networking, harness
installation, authentication, and project provisioning remain user responsibilities.

Authenticate providers independently on each host; do not copy stored credentials
between hosts. `remote-agent pi-login PROVIDER --host HOST` starts Pi provider
login over SSH. `remote-agent claude-account --login PROFILE --host HOST` starts
Claude login; account switching is host-global and may affect running sessions.

## Optional periodic checks

Deployment installs `periodic-checks.ts` as a user-level Pi extension on both
the controller and selected remote host, symlinked at
`~/.config/pi/agent/extensions/periodic-checks.ts`. If `PI_CODING_AGENT_DIR` is
set, its `extensions/` directory is used instead. Unmanaged files and foreign
symlinks at the extension path are not replaced.

For local-only extension installation, run from this checkout:

```sh
python3 -c 'import deploy; deploy.install_periodic_extension()'
```

This helper performs no remote deployment. Importing `deploy.py` still requires
a readable, valid `~/.config/remote-agent/remote-hosts.json`. To also link the
local controller, first ensure `~/.local/bin` exists, then use
`python3 -c 'import deploy; deploy.link_controller()'` instead; that helper also
installs the extension. These are Python helpers, not CLI flags. Keep this
checkout in place because the installed paths are symlinks.

At session start, the extension discovers the current directory's Git root and
looks for `periodic-checks.md` there. Keep this host-local policy file Git-ignored
and configure its contents independently on each host. Deployment ships plugin
code, never project checklist contents. That file opts the project in; no file
means no timer. Start a new Pi session after installing the extension or adding the
checklist. Eligible local and remote main sessions behave alike: TUI or RPC
mode, a multi-agent store, and active `spawn_agent` and `send_agent_message`
tools are required. Child sessions are excluded. These are custom Pi
multi-agent capabilities; compatibility with stock Pi is not guaranteed.

Use Google OKF YAML frontmatter inside the ignored, host-private
`periodic-checks.md`, followed by the audit checklist:

```markdown
---
type: Periodic Checks
interval_seconds: 1800
---
# Periodic checks

Report Git status and test failures. No edits.
```

`type` must be a nonempty string; custom `interval_seconds` must be a positive
safe integer, bounded to 1–2147483 seconds by Node's timer limit. Unknown fields
are allowed. For manual testing, use `interval_seconds: 60`. The extension reuses
Pi's exported `parseFrontmatter` YAML parser; no new dependency is added.

Metadata is read on `session_start` (including reload), not on every tick.
The first tick waits the full configured interval; subsequent ticks use that
interval while the session is alive. There is no fixed 30-minute fallback.
Missing or invalid metadata produces an explicit error and leaves the timer
disabled; an absent file means no timer. Reload or start a new session to apply
metadata edits or re-arm after restoring the file. Busy sessions, pending
messages, or an existing live audit skip a tick. Removing the checklist stops
the timer at the next tick. This is not an offline scheduler.

Each dispatched prompt requests one bounded, read-only audit child that rereads
the checklist and sends findings to the originating main session. Audits are
report-only: no cleanup, blocking, cancellation, steering, edits, or deployment.
Authorized work continues independently.

## Tests

Run from this checkout:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
NODE_PATH="$(npm root -g)${NODE_PATH:+:$NODE_PATH}" node --test tests/test_periodic_scheduler.mjs
```

Python discovery includes `test_remote_agent.py` and `test_unc_mapping.py`.

Native Node scheduler tests require a Node version that can load TypeScript and
an installed `@earendil-works/pi-coding-agent` npm package reachable through Node
resolution. The command above exposes global npm packages through `NODE_PATH`.
If Pi's npm package is installed elsewhere, set `NODE_PATH` to its containing
`node_modules` directory instead. A compiled-only Pi executable is insufficient
for these tests; they do not universally run with just `pi` installed.

## License

[MIT](LICENSE).

## Scope and documentation limits

This README documents the generic controller workflow, not a fully portable
installation contract.

The [configuration spec](docs/specs/configuration.md) tracks mapping behavior and
its tests. Host-specific provisioning and transport readiness must be established
for each installation; examples here are not evidence of a provisioned host.

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
commands when starting on a non-default host.

New sessions use dtach and save an explicit `backend: dtach` record. The remote
worker requires dtach; a missing executable fails instead of falling back to tmux.
Existing records without a backend retain tmux behavior, including conversation
restart. Installing the worker does not migrate running sessions.

For dtach, `status` and `list` use private runtime metadata and socket state to
report liveness, attachment and a recorded exit status when available. Saved dtach
records remain listed after exit; `locate`/attachment can find them for recovery.
`logs` fails explicitly: dtach retains no terminal output and provides no replay.
Legacy tmux sessions retain their existing pane-history path; that output may
contain sensitive content.

Cancel kills the selected dtach master and owned process tree, then removes its
private state. Process identities are checked to avoid signalling recycled PIDs;
a live identity mismatch refuses cancellation. Legacy cancellation kills its
tmux session and removes saved state. Neither route cleans up project files.

Control requests use SSH. Interactive attachment uses Mosh with SSH bootstrap,
requires reachable Mosh UDP ports, and has no SSH fallback. Attachment retries
dropped connections; the worker can resume a recorded conversation after its
session is lost. This does not guarantee recovery of unsaved work or availability
of the host, harness, or provider.
Mosh retains `--no-init` for terminal-native scrollback. dtach attachment uses
`-r winch`; press Ctrl+backslash to detach without stopping the agent. No Ctrl+L
is injected. Same-size reattachment may need a window resize to repaint Pi.
Before each TTY attachment and every retry, the controller retrieves Pi's saved
current, compaction-aware history over SSH, prints it locally into scrollback,
and adds viewport-height padding before starting Mosh. Non-TTY output skips
preloading. The worker invokes the installed `pi --export-terminal ID
--export-columns WIDTH`; it does not parse transcripts itself. This
avoids Mosh dropping intermediate screens during a large redraw. It preserves
existing terminal scrollback; reconnecting in the same terminal can repeat a
previously restored snapshot. Terminal capacity still limits retained history.

This is Pi's loaded context with normal collapsed output, not the pre-compaction
archive or a recording of detached terminal bytes; images and expanded tool
output are not restored. Unsupported harnesses, missing session/conversation
records, and confirmed live sessions without saved history show an explicit
notice; other export errors
stop attachment rather than silently omitting history. Live output still uses
Mosh and can lose intermediate screen states during fast output or network gaps.
The controller forwards its `COLORTERM` capability on start and attach. Dtach
launches use `TERM=xterm-256color`; truecolor is advertised only when the caller
reports `truecolor` or `24bit`. Attachment saves the current capability for the
next launch, refreshing the saved value before any restart; it does not change a
running agent's environment. No palette or theme edit is required.

Reopen a saved conversation with normal `remote-agent attach NAME`; no manual
path, environment, or session repair is required. With resource scoping enabled,
restarts reuse the same `remote-agent-NAME-dtach.scope`, preserving retained jobs
and existing limits rather than killing jobs or changing caps. An active delegated
scope receives only the new launcher through systemd's `AttachProcessesToUnit` API.
An active older nondelegated scope is upgraded in place using `systemctl edit
--runtime --stdin` and a manager reload: one fixed
`50-remote-agent-delegate.conf` drop-in, not a new name or file on every reopen.
When the scope is inactive, the worker removes only that owned runtime override
and recreates the same unit with delegation; other drop-ins remain untouched.
Startup readiness is published only after scope creation or attachment succeeds;
systemd failures are reported, not treated as a successful restart.
See the [session backend contract](docs/specs/session-backends.md).

### Another user's sessions

On a native Linux host, an administrator can use the configured SSH account to
inspect or attach another Unix user's managed sessions:

```text
remote-agent list --user researcher
remote-agent attach task --user researcher
```

`--user` is available only for `list` and `attach`. The configured SSH account
must already have `authsudo -u USER` authorization; this option grants no access
and never changes policies or SSH keys. Wrapped/Windows hosts are unsupported.

Deploy the updated helper for the configured SSH account with `./deploy.sh`.
To keep another user's worker current, configure the selected host's
`deploy_users` as described under [Deploy](#deploy).
The target user must already have their own host/project maps; the selected
`--host` key must exist in their host map. No maps or credentials are
copied by listing or attaching. The target home is resolved from the server's
Unix account directory, not by changing the controller's configured `home`.

The SSH/Mosh transport remains authenticated as the configured administrator.
The worker and attachment client run as the selected user with their HOME, runtime
and Pi config directory; administrator credentials and tmux environment are not
inherited. Listing includes a `user` field. Attachment checks authorization and
configuration before entering the Mosh reconnect loop and remains interactive,
with the existing detach/recovery behavior. It does not discover standalone Pi
processes or arbitrary tmux sessions outside the `remote-agent-*` namespace.
Existing live cross-user acceptance covers tmux, not native dtach attachment;
the identity helper is unchanged, but that is not proof of dtach cross-user support.

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

## Clipboard images

```text
remote-agent paste
remote-agent paste --host agent-server
```

`paste` uploads the controller's clipboard image to the selected remote host.
On macOS it uses the built-in `osascript` and AppKit pasteboard APIs to convert
clipboard images to PNG; no extra clipboard package is needed. On Linux it
requires `wl-paste` from `wl-clipboard` and an image available as PNG.
An empty, non-image, or unreadable clipboard fails before upload.

After uploading, use the remote harness's image-paste shortcut (Ctrl+V in Pi).
This command does not paste text or send a keyboard shortcut into the session.
The remote worker and its deployed `wl-paste` shim remain Linux-side components.

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

Optional host `deploy_users: ["researcher"]` updates explicitly listed existing
Unix accounts after the default SSH account, in list order, using the same
payload and revision through existing `authsudo -u USER` authorization. No
accounts are discovered. Target homes come from the remote Unix account
directory. Both target mapping files must already exist: deployment preserves
their bytes and permissions rather than copying the operator's maps. Each target
runs with an isolated environment including its own `HOME`, `USER`, `LOGNAME`,
`XDG_RUNTIME_DIR` and `PI_CODING_AGENT_DIR`; operator credentials are not inherited.
A failed target stops deployment with an explicit error; earlier account updates
are not rolled back. Missing or empty `deploy_users` retains default-account-only
behavior. Configured cross-user deployments reject wrapped/Windows hosts.
Deployment does not restart services or sessions or modify saved conversations.

Deployment preserves existing `~/.tmux.conf`, including a symlink to the deployed
legacy `tmux.conf`: that target's existing contents are preserved too. It does not
reload tmux configuration or change running sessions. New dtach resource scopes
use a separate `remote-agent-NAME-dtach` unit rather than the legacy session unit.
It links `~/.local/bin/wl-paste` to the shipped worker, refusing to replace an
unmanaged file or foreign symlink at that path.
It preserves an existing system or user `agents.slice`; otherwise it installs
the bundled user slice and reloads the user manager. Review its limits for your
host. System-scope authorization, SSH configuration, Mosh networking, harness
installation, authentication, and project provisioning remain user responsibilities.

Authenticate providers independently on each host; do not copy stored credentials
between hosts. `remote-agent pi-login PROVIDER --host HOST` starts Pi provider
login over SSH. `remote-agent claude-account --login PROFILE --host HOST` starts
Claude login; account switching is host-global and may affect running sessions.

### Claude account freshness

The active credentials are `~/.claude/.credentials.json`; `.active-profile`
records their owner, and `<PROFILE>.credentials.json` stores each named account.
Switching or releasing an account saves global credentials only when the named
file is absent or its modification time is no newer than the global file.
Equal timestamps prefer global credentials. A newer named refresh is never
replaced by stale global credentials.

Selecting the already-active profile still adopts newer named credentials.
Credential copies use atomic private-file replacement and preserve the source's
pre-copy modification time, rather than making a copied token look freshly
refreshed. Byte-identical writes leave the destination inode and modification
time unchanged. These rules select freshness by filesystem timestamps, not by
OAuth token validation; account switching is not synchronized with running
provider refreshes.

Isolated OAuth-shaped fixtures in `tests/test_claude_account_freshness.py` cover
stale-global protection, native global refresh persistence, same-profile adoption,
unchanged saves, equal-time ties, release, and first save. Run only account tests:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -k ClaudeAccount -v
```

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
means no timer. Reload an existing Pi session or start a new one after installing
the extension to discover newly installed commands, or after adding the checklist
to arm its timer. Eligible local and remote main sessions behave alike: TUI or RPC
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
metadata edits or re-arm after restoring the file. Main-session activity and
pending messages do not skip ticks. A pending spawn or an existing live audit
skips overlapping dispatch. Removing the checklist stops
the timer at the next tick. This is not an offline scheduler.

Use `/periodic-checks status` (or `/periodic-checks` with no arguments) to display
scheduler state. Use `/periodic-checks run` to request an audit immediately.
Other arguments show `Usage: /periodic-checks status|run`.
The notification reports `Periodic checks: armed` or `Periodic checks: disabled`,
plus `Checklist:` when its path is known. Armed status includes the active
`interval_seconds:` and a relative schedule estimate: `Next run: in N minutes`,
`Next run: in 1 minute`, or `Next run: in less than a minute`. Remaining time is
calculated when status is requested; minutes round up (90 seconds shows 2
minutes), and due or overdue ticks show less than a minute. Disabled status
includes `Reason:` (for example, `No checklist` or `Checklist removed`).
Status reflects the current scheduler, not unapplied metadata edits. The command
is read-only and displays its notification immediately, including while main is
busy: no audit, abort, model turn, timer reset, or re-arm. The next-run estimate
tracks the scheduled boundary, not child completion or a guaranteed dispatch;
skipped ticks still advance that boundary.

`/periodic-checks run` uses the armed schedule's same direct, read-only child
dispatch, even while main is busy or has pending messages. It does not reset or
advance the normal scheduled countdown, reread metadata, or re-arm a disabled
schedule. A pending spawn or live `periodic-checks-audit` refuses duplicate runs
with `Cannot run periodic audit: Audit already starting` or `Audit already live`.
Disabled schedules, missing/removed checklists, unavailable runtime/tools, and
changed session/project identity produce a reason-bearing warning rather than
claiming success. Removing the checklist during a manual run also stops the timer.

An accepted manual request first reports `Periodic audit starting`. Only a
successful tool result with `details.dispatched: true` reports `Periodic audit
started`; tool errors, blocked/undispatched results, and rejection report a
`spawn_agent` error with session ID, checklist path, and failure details. The
command does not wait for spawn settlement or child completion. `started` means
dispatch accepted, not audit completed or findings delivered.

The scheduler calls `pi.callTool("spawn_agent", ...)` directly to launch one
fresh-context `explore` child named `periodic-checks-audit`, independently of
main's current turn. It sends no follow-up/steering prompt to main and queues no
model prompt for main to spawn the audit. The pending-spawn lock releases when
the tool call settles, including errors; dispatch failures produce an explicit
error notification. A child with that name blocks overlap until its stored
lifecycle is `completed`, `failed`, or `aborted`. Completion permits a later
scheduled tick, not an immediate audit; main's turn ending does not release it.
A restored live audit also blocks overlap after reload. Unrelated children do
not block dispatch. Late spawn results from an old schedule cannot unlock the
new schedule or emit stale diagnostics.

Each audit rereads the checklist and sends findings to the originating main
session, checking successful delivery. Audits are report-only: no corrections,
nested audit spawning, builds, cleanup, blocking, cancellation, steering, edits,
or deployment. Authorized work continues independently. Offline real-Pi PTY
fixtures prove scheduled/manual audits and visible status during active model
and tool turns, overlap refusal, unchanged countdown, and normal main completion.
Findings are delivered while main is busy; main consumes its mailbox when idle.

## Tests

Run from this checkout:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
NODE_PATH="$(npm root -g)${NODE_PATH:+:$NODE_PATH}" node --test tests/test_periodic_scheduler.mjs
```

Python discovery also includes `test_periodic_runtime.py`, which uses an installed
Pi executable (`PI_TEST_BINARY` overrides discovery), temporary private state,
and a faux provider without network requests or credentials. Its three cases
exercise scheduled audits during held model/tool turns and manual-run dispatch.

Native Node scheduler tests require a Node version that can load TypeScript and
an installed `@earendil-works/pi-coding-agent` npm package reachable through Node
resolution. The command above exposes global npm packages through `NODE_PATH`.
If Pi's npm package is installed elsewhere, set `NODE_PATH` to its containing
`node_modules` directory instead. Python's embedded Node runner needs the same
`NODE_PATH`. A compiled-only Pi executable is insufficient
for these tests; they do not universally run with just `pi` installed.

History-attachment tests additionally use `@xterm/headless` 5.5.0 to assert actual
normal-buffer scrollback, not merely emitted bytes. It is a test-only dependency
already present in the Pi checkout's development `node_modules`; expose that
location through `NODE_PATH` when running those tests.

## License

[MIT](LICENSE).

## Scope and documentation limits

This README documents the generic controller workflow, not a fully portable
installation contract.

The [configuration spec](docs/specs/configuration.md) tracks mapping behavior and
its tests. Host-specific provisioning and transport readiness must be established
for each installation; examples here are not evidence of a provisioned host.

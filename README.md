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

Optional project `pi_executable` selects an absolute **host** executable path for
Pi only, for example `"pi_executable": "/home/developer/bin/sample-pi"`.
The path must be a nonempty string naming an existing executable regular file;
invalid configured values fail explicitly without falling back to `pi` on PATH.
When absent, Pi resolution is unchanged; Claude ignores this setting. New Pi
sessions, explicit resumes, saved-conversation restarts, and terminal-history
exports all use the project's current setting, preserving the mapped working
directory, PATH, and environment. A host wrapper may forward argv to `docker exec`;
it must support Pi's launch/resume and `--export-terminal` arguments. This setting
does not provision containers or provide isolation.

Optional project `pi_cancel_executable`, for example
`"pi_cancel_executable": "/home/developer/bin/sample-pi-cancel"`, uses the same
absolute existing executable regular-file validation. On cancellation of a
recorded Pi session, the worker invokes `[path, conversation_id]` without a shell,
after host tmux/dtach shutdown and before deleting saved state. The recorded
conversation ID must satisfy the same validation as request resume IDs. The hook
also runs when the host session is already dead: stopping a Docker client does
not prove its container process stopped. For one container per conversation,
the hook must remove the container identified by that conversation's deterministic
name and succeed if it is already absent. Nonzero exit fails cancellation
explicitly and preserves saved state for retry, without fallback. Process identity
mismatch in dtach refuses cancellation before running the hook. Restart never
runs it. Without this setting, cancellation is unchanged; Claude ignores it.

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

## Rust prototype

`remote-agent-rs` at `28c9a90` is an undeployed Pi-only prototype. It does **not**
replace the operational Python `remote-agent`, detach or Mosh stack documented
below. Linux server fixtures have passed; actual Pi login/paste, installed
attachment and macOS build/runtime acceptance remain open.

Build locally with a Rust toolchain that supports edition 2024:

```text
cargo build --locked --bin remote-agent-rs
./target/debug/remote-agent-rs --help
./target/debug/remote-agent-rs projects
```

The prototype reads the same private host/project maps described above on each
account. Remote commands require the matching Rust executable already available
at the selected account's `~/.local/bin/remote-agent-rs`, Pi, the mapped project,
SSH access and direct UDP reachability to the server's dynamically selected QUIC
port. Do not replace existing sessions to try it.

### Parallel Rust installation

Run `./deploy.sh --rust [--host HOST]` to install only `remote-agent-rs`, beside
Python `remote-agent`. The default deployment mode remains unchanged. Rust mode
preserves both maps on the local, default SSH, and configured `deploy_users`
accounts; missing maps fail instead of copying the operator's configuration.
It does not change Python workers, detach/Mosh, services, or running sessions.

The local client requires Cargo/rustc with edition 2024 support. Supported native
platforms are Linux GNU and macOS, on x86_64 or aarch64/arm64; the server must be
unwrapped Linux GNU. Each build uses the explicit native rustc host target and
`Cargo.lock`. A matching Linux client/server OS and architecture builds once
locally. Otherwise, the primary SSH account builds the bundled source and lockfile
in a temporary directory, with its own Rust toolchain; that Linux binary is then
sent to all configured accounts. Cargo dependencies must be available from cache
or the configured registry. No repository clone or build-failure fallback occurs.

Cross-user installation uses existing `authsudo` authorization and the primary
account's already installed `remote-agent-user.py` environment helper. Provision
that helper through the existing Python deployment before using this mode.

Each account receives a private, content-addressed executable at
`~/.local/share/remote-agent-rs/<sha256>/remote-agent-rs`, selected by
`~/.local/bin/remote-agent-rs`. Directory/file modes are `0700` for the store and
`0500` for each version and binary. Updates retain old versions without rewriting
them. Unmanaged executables and foreign or dangling symlinks are rejected.
`tests/test_rust_deploy.py` covers the installation with fixture SSH/build tools;
it does not establish native macOS builds or live cross-user deployment.

For a separately provisioned experimental endpoint:

```text
./target/debug/remote-agent-rs --host agent-server start sample rust-demo --history-bytes 16777216
./target/debug/remote-agent-rs --host agent-server list
./target/debug/remote-agent-rs --host agent-server attach rust-demo
./target/debug/remote-agent-rs --host agent-server logs rust-demo
./target/debug/remote-agent-rs --host agent-server pi-login PROVIDER
```

Other commands are `status NAME` and `cancel NAME`; cancel stops the experimental
server and its owned PTY process. Start accepts `--resume CONVERSATION_ID` and
`--prompt-file PATH`. `--user USER` uses existing `authsudo` access and assumes
`/home/USER`; cross-user acceptance is not established. Ctrl+backslash detaches;
Ctrl+V requests the local image without a separate `paste` command. Clipboard
providers are `wl-paste` (Wayland), `xclip` (X11), or `osascript` (macOS).

History and replay-journal budgets are each 16 MiB by default and at most;
`--history-bytes` can reduce them. Fresh attachment resets outer-terminal
scrollback. Alternate-screen applications and unknown VT callbacks are unsupported.
See [implementation and proof limits](docs/wiki/systems/unified-pi-session.md)
and the single [Cargo dependency inventory](docs/wiki/systems/unified-pi-session.md#cargo-dependency-inventory).

### Rust CI proof

`.github/workflows/rust.yml` runs formatting, `cargo check --locked`, and
`cargo test --locked` on GitHub-hosted Ubuntu and macOS. The macOS job then
prepares an owned PNG in its disposable clipboard and explicitly runs the
otherwise ignored native `osascript`/JXA clipboard test. Decoded dimensions and
RGBA pixels are compared, not PNG encoding bytes. Normal tests do not read or
write a user's clipboard.

The transport tests use production `client::connect` and protocol framing with
an actual loopback QUIC test peer, including rejection of a different TLS
certificate. They do not use the Linux production server. Passing macOS CI
proves native client compilation, loopback transport, and fixture clipboard
conversion; it does not prove macOS-to-Linux attachment, interactive Pi, SSH,
terminal repaint, or installed deployment. These CI tests are added but actual
macOS execution must be confirmed from the workflow results.

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
worker runs the `detach` executable, a dtach-compatible rewrite
(`~/Projects/cli/detach`) that delivers the complete output stream to every
attached client; dtach 0.9 drops output for a client that is not writable while
another is. A missing `detach` fails instead of falling back to tmux. Masters
started by C dtach keep its behavior until restarted; `detach -a` attaches to
them. Existing records without a backend retain tmux behavior, including
conversation restart. Installing the worker does not migrate running sessions.

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
is injected. Pi repaints its viewport on same-size reattachment.
After deploying the bracketed-paste repair, detach an existing dtach attachment
with Ctrl+backslash and reconnect using the same `remote-agent attach NAME`
command (including `--user` when applicable); no agent restart is needed.
Keep paste safety warnings enabled; this repair enables bracketed paste rather
than disabling warnings. See [paste acceptance](docs/specs/session-backends.md#tests-asserting-this-spec)
for regression and installed SSH/Mosh/Pi evidence.
Before each TTY attachment and every retry, the controller retrieves Pi's saved
current, compaction-aware history over SSH, prints it locally into scrollback,
and adds viewport-height padding before starting Mosh. Non-TTY output skips
preloading. On terminals supporting the xterm title stack, TTY attachment sets
the window title to the saved project name and restores the previous window title
on detach or failure. The icon title is unchanged. Each retry reapplies the project title; the attached TUI may override it.
No title is invented for a missing session record. The worker invokes the installed `pi --export-terminal ID
--export-columns WIDTH`; it does not parse transcripts itself. This
avoids Mosh dropping intermediate screens during a large redraw. It preserves
existing terminal scrollback; reconnecting in the same terminal can repeat a
previously restored snapshot. Terminal capacity still limits retained history.

When a terminal with available Pi history changes size (including font zoom), the
controller briefly reconnects Mosh after the size settles, fetches fresh history
at the new width, and replaces the terminal's scrollback before restoring the
live view. This deliberately discards unrelated pre-attach scrollback as well as
stale transcript copies. It does not restart Pi, resubmit input, or change the
conversation. Same-size resize signals do not reconnect. An unavailable or failed
refresh stops attachment without clearing the old scrollback. Initial attachments
without available history retain ordinary Mosh resizing. Saved history can overlap
the live viewport's most recent messages; the rebuild prevents older resize
artifacts accumulating, not that snapshot/live-view overlap.
Detach an existing controller with Ctrl+backslash and run the same
`remote-agent attach NAME` command (including `--user` when needed) once to enable
resize rebuilding. No Pi restart or remote deployment is needed for this
controller-only repair. Resize coverage includes real controller PTYs with
SSH/Mosh fixtures and native Mosh/Pi TUI regression coverage; installed default
and cross-user SSH/Mosh/Pi acceptance proves terminal-buffer behavior, not desktop
pixels. See [acceptance scope](docs/specs/session-backends.md#known-gaps-current-cycle).

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
both backends reuse their stable unit: `remote-agent-NAME.scope` for legacy tmux,
`remote-agent-NAME-dtach.scope` for dtach. Retained detached jobs do not require
manual scope cleanup before restart. See the
[session backend contract](docs/specs/session-backends.md) for scope reuse,
limits, delegation and startup guarantees.

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
Live cross-user acceptance includes one user-authorized SSH/Mosh/dtach Pi
attach/detach at 48×200, returning exit 0. Process and health metadata confirmed
the target working directory and selected conversation; the existing tmux client
was preserved. Earlier disposable tmux acceptance separately proved same-named
account isolation. This is not an exhaustive cross-user dtach isolation matrix
or full-history pixel proof; see [acceptance scope](docs/specs/cross-user-attachment.md#tests-asserting-this-spec).

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

`deploy.sh` deploys remotely first, then links the local controller. Local-only
users should not run it blindly; controller linking is independent of Pi
extension installation.

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
Claude login directly in that named account's canonical directory. Selection
changes only after successful login; failed login preserves the old selection.

### Shared Claude accounts

Each account owns one grant at
`~/.config/pi-claude-bridge/profiles/PROFILE/.credentials.json`. Bridge, AskClaude,
and new native Claude launches share that directory as `CLAUDE_CONFIG_DIR`.
Native Claude ignores inherited API/OAuth token overrides; Pi keeps caller env.
`~/.claude/.active-profile` selects an account for new native launches. Switching
changes only this file; existing sessions are not restarted or migrated.
Listing reads canonical profile directories, including logged-out accounts.
Selection and native launch require a nonempty canonical OAuth access token.

Bridge owns one-time legacy migration. Prepare existing profiles before
activating this worker: `.credential-owner` must contain `shared-v1\n`.
Missing/empty canonical grants are not restored from old global/named files.
Login creates a private `0700` canonical directory and ownership marker, even
when login fails, and can claim an existing unmarked directory for explicit
reauthentication without importing legacy grants or changing canonical bytes
before native login. Invalid existing markers and symlink profile directories
fail explicitly. Initialization uses the bridge's
exclusive `.credential-migration` directory lock; existing locks are not removed.

`--name-current PROFILE` can move an unnamed global login into a previously
absent canonical account once. It validates a nonempty OAuth access token and
refuses existing accounts, including cleared ones. No action clears the
selection, so retained legacy global grants never become launch credentials.
There is no routine copying, freshness comparison, or writeback.

Local synthetic subprocess fixtures in `tests/test_claude_account_freshness.py`
cover switching, cleared grants, import, launcher environment, named
login, failed login, and migration-lock refusal without network/auth calls.
Run only account tests:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -k ClaudeAccount -v
```

## Optional periodic checks

Pi bundles `periodic-checks` and `exit-command` as first-party extensions.
Remote-agent no longer installs `periodic-checks.ts` on the controller or remote
hosts. Update Pi on each host; start a new session or use `/reload` to discover
`/periodic-checks` and `/exit`.

After installing the bundled Pi build, remove superseded user-level
`periodic-checks.ts` and `exit-command.ts` copies/symlinks from the active Pi
configuration's `extensions/` directory and remove explicit references to those
old paths. Keep only the bundled registrations; do not load both versions.

Periodic checks remain opt-in through an ignored, host-private
`periodic-checks.md` at the current Git root. Each host maintains its own file;
remote-agent never distributes it. No file means no timer or audit.

Pi owns the [scheduler and report-only audit contract](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md),
[checklist configuration, commands, discovery, and disabling](https://github.com/Osso/pi/blob/master/docs/wiki/systems/periodic-checks.md),
and [`/exit` contract](https://github.com/Osso/pi/blob/master/docs/specs/slash-commands.md#bundled-exit).

## Tests

Run from this checkout:

```sh
./run-tests.sh
./run-tests.sh unit test_resize_attach.py test_history_attach.py
```

Native scheduler and dispatch tests belong to Pi; see the
[canonical scheduler spec](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md#tests-asserting-this-spec).
Python discovery retains `tests/test_periodic_runtime.py` with
`tests/fixtures/periodic-runtime-provider.ts` as installed-Pi integration
acceptance. It uses Pi's bundled extension without explicit `--extension`
injection, private temporary state, and an offline faux provider. Set
`PI_TEST_BINARY` to select the installed Pi under test. These cases cover
scheduled/manual audits and status during held model/tool turns; this documentation
update does not claim they pass against the new bundle.

History-attachment tests additionally use `@xterm/headless` 5.5.0 to assert actual
normal-buffer scrollback, not merely emitted bytes. It is a test-only dependency
already present in the Pi checkout's development `node_modules`; expose that
location through `NODE_PATH` when running those tests.
The native resize regression additionally needs Node 22.18+, Linux, Mosh client
and server, `DETACH` pointing to the Rust executable, and `PI_TUI_SOURCE` pointing
to Pi's `packages/tui/src`. Run `./run-tests.sh unit test_mosh_resize.py` with those
environment variables; missing native dependencies are reported as skipped.
This test uses only disposable localhost sessions, not a provider or real SSH
account.

## License

[MIT](LICENSE).

## Scope and documentation limits

This README documents the generic controller workflow, not a fully portable
installation contract.

The [configuration spec](docs/specs/configuration.md) tracks mapping behavior and
its tests. Host-specific provisioning and transport readiness must be established
for each installation; examples here are not evidence of a provisioned host.

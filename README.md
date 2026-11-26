# remote-agent

Rust client (`remote-agent`) and Linux session server (`remote-agent`) for
explicitly requested Pi sessions. Each session runs Pi in a PTY under a
per-session systemd user service; the client attaches over QUIC after an SSH
control request. Commands inspect, attach to, restart or cancel sessions on one
selected host and account. This is not a turnkey host provisioner or a
cross-host session discovery service.

## Prerequisites

- Client: Cargo/rustc with edition 2024 support, OpenSSH client, and direct UDP
  reachability to the server's dynamically selected QUIC port.
- Server: unwrapped Linux GNU with SSH access, systemd and a usable user
  manager. Enable linger if sessions must survive logout or reboot.
- Install and authenticate Pi independently on the server account. Pi and its
  configuration are user-provisioned, not installed by deploy.
- Create project checkouts at their mapped remote paths, manually or with
  `remote-agent create`. Provision project build tools, dependencies, permissions
  and resource limits separately.

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
Pi, for example `"pi_executable": "/home/developer/bin/sample-pi"`.
The path must name an existing executable regular file; invalid configured
values fail explicitly without falling back to `pi` on PATH. New sessions,
explicit resumes and restarts use the project's current setting. A host wrapper
may forward argv to `docker exec`; this setting does not provision containers
or provide isolation.

Optional project `pi_args` replaces the default Pi session arguments. Values
must be a non-empty array of non-empty strings. For example,
`"pi_args": ["questions"]` runs `pi questions` without `--session-id`,
conversation ID, or a start prompt. The remote-agent session still has its
configured name and remains available to `attach NAME` and the session switcher;
the conversation ID remains management metadata.

Optional project `pi_cancel_executable`, for example
`"pi_cancel_executable": "/home/developer/bin/sample-pi-cancel"`, uses the same
absolute existing executable regular-file validation. `cancel NAME` invokes
`[path, conversation_id]` without a shell after the session service stops. The
hook also runs when the session is already dead: stopping a Docker client does
not prove its container process stopped. For one container per conversation,
the hook must remove that conversation's container and succeed if it is already
absent. Nonzero exit fails cancellation and preserves state for retry. Restart
never runs it.

Host-targeting commands default to the `agent-server` key. Pass `--host HOST`
to select another configured key; no automatic rerouting or aggregate listing
occurs. `remote-agent projects` lists the selected remote account's project
map, not checkout availability. An SSH destination may be an alias configured separately
in SSH configuration. `REMOTE_AGENT_<HOST>_SSH` overrides that host's destination
(uppercase host key, with hyphens replaced by underscores).

Host `cargo_build_jobs` (a positive integer string) is passed as
`CARGO_BUILD_JOBS` to Pi sessions. Optional host `build_host_scripts` is passed
unchanged as `BUILD_HOST_SCRIPTS`. Set an absolute remote path only when your projects require
it; no project-specific path is injected otherwise.

## Usage

At `fdaa0d6`, deployment and real SSH/QUIC/Pi attachment were verified on
agent-server for `osso` and `allison`. An actual host reboot was not performed. GitHub run `40226091616` passed the Ubuntu and
macOS jobs at `fdaa0d6`. See
the [proof matrix](docs/wiki/systems/unified-pi-session.md#evidence-and-remaining-acceptance).

Build locally with a Rust toolchain that supports edition 2024:

```text
cargo build --locked --bin remote-agent
./target/debug/remote-agent --help
./target/debug/remote-agent projects
```

Each account reads its own private host/project maps. Remote commands require
`~/.local/bin/remote-agent`, Pi and the mapped project on the selected account.

### Installation

Run `./deploy.sh --client-only` to install the local client without deploying
the server.

Run `./deploy.sh [--host HOST]` to install the client locally as
`~/.local/bin/remote-agent`. Server accounts get `~/.local/bin/remote-agent`,
which the session unit runs. Deploy copies both local maps byte-for-byte to the
default SSH account's `~/.config/remote-agent/` (`0700`/`0600`). Optional host
`deploy_users: ["researcher"]` also installs to listed existing Unix accounts
through `authsudo -u USER` with an isolated environment; their maps must already
exist and are preserved. Missing maps fail explicitly.
On every server account it installs the repository-root `remote-agent@.service`
template into `~/.config/systemd/user/` and runs
`systemctl --user daemon-reload`. Running sessions keep their own binary.

The local client requires Cargo/rustc with edition 2024 support. Supported native
platforms are Linux GNU and macOS, on x86_64 or aarch64/arm64; the server must be
unwrapped Linux GNU. Each build uses the explicit native rustc host target and
`Cargo.lock`. A matching Linux client/server OS and architecture builds once
locally. Otherwise, the primary SSH account builds the bundled source and lockfile
in a temporary directory, with its own Rust toolchain; that Linux binary is then
sent to all configured accounts. Cargo dependencies must be available from cache
or the configured registry. No repository clone or build-failure fallback occurs.

Cross-user installation uses existing `authsudo` authorization. Runtime Rust
account routing uses the native helper described in the
[configuration contract](docs/specs/configuration.md#rust-account-and-launch-policy).

Each account receives a private, content-addressed executable at
`~/.local/share/remote-agent/<sha256>/remote-agent`, selected by
`~/.local/bin/remote-agent` locally and `~/.local/bin/remote-agent` on servers. Directory/file modes are `0700` for the store and
`0500` for each version and binary. Updates retain old versions without rewriting
them. Unmanaged executables and foreign or dangling symlinks are rejected.
`tests/test_deploy.py` covers deployment with fixtures; real two-account deployment
and attachment at `fdaa0d6` are recorded in the proof matrix.

```text
remote-agent --host agent-server start sample demo --history-bytes 16777216
remote-agent --host agent-server list
remote-agent --host agent-server attach demo
remote-agent --host agent-server logs demo
remote-agent --host agent-server login PROVIDER
```

`create PROJECT --local PATH [--remote PATH]` prepares a new project on both
sides, for example:

```text
remote-agent create creatures --local /syncthing/Sync/Projects/creatures
```

It creates the local checkout and the remote checkout (`Projects/PROJECT` by
default), initializes `master` in both with `git init`, and registers the
mapping in the local and selected remote account's `remote-projects.json`.
The local path must be absolute; the remote path must be relative to the remote
account's home. Both project maps must already exist. When a mapping is new,
an existing checkout path fails without overwriting it; a different mapping under
the same name also fails. On a repeat, an identical local mapping is accepted
without checking its checkout; an identical remote mapping returns `"created": false`
only if its path is a directory. Map writes use mode `0600`. Local preparation
happens first and is not rolled back if remote creation fails; retrying can
complete the remote side. The command does not commit, push, host a repository,
or start a session.

`restart NAME` targets the selected `--host` and `--user` account's systemd
session service and resumes its saved conversation. Restart interrupts foreground
work and jobs in the service control group; those jobs, in-progress model turns
and unsent editor text are not restored.

`start` marks the session `started` in its `launch.json`; `list` and `status`
report the mark. `stop NAME` disables and stops the unit, waits for the server
to exit, then clears the mark; it keeps the conversation and runs no hook.
`start PROJECT NAME` on an existing unmarked session marks it and resumes its
saved conversation (a running session recorded before the mark existed is only
marked). `start NAME` naming an existing session uses its recorded project, and
`restart NAME` on an unmarked session starts it the same way. Restart and deploy sweeps
must restart only sessions marked `started`.

Every remote-agent SSH call shares one master connection per destination
(`ControlMaster=auto`, socket `~/.ssh/remote-agent-%C`, `ControlPersist=10m`), so
only the first call after ten idle minutes pays the SSH handshake.

`rename NAME NEW_NAME` stops the session, moves its state directory to the new
name, and starts the `remote-agent@NEW_NAME` unit again only if it was marked
`started`, resuming the same saved conversation. An existing `NEW_NAME` is refused
before any change. Attached clients disconnect; attach again with the new name.

Other commands are `status NAME` and `cancel NAME`; cancel also clears the mark. It stops the
enabled user unit with `systemctl --user disable --now`, waits for the server PID
to exit, then runs the project's `pi_cancel_executable` hook with the conversation
ID. Repeating the unit stop is a no-op, so a failed hook can be retried.
Identity or teardown errors retain state for retry. Start accepts `--resume CONVERSATION_ID` and
`--prompt-file PATH`. `--user USER` uses native passwd lookup and a clean target-account environment
through existing `authsudo` authorization; real attachment was verified for
`osso` and `allison` at `fdaa0d6`. See [launch policy](docs/specs/configuration.md#rust-account-and-launch-policy). Ctrl+backslash detaches;
Ctrl+V sends the local clipboard image to the session. Clipboard
providers are `wl-paste` (Wayland), `xclip` (X11), or `osascript` (macOS).

`start` enables and starts `remote-agent@NAME.service` in the user's
`remote-agent.slice`, apart from `agents.slice`, so freezing or capping agent
workloads cannot suspend a session. Enabled units return when the user manager starts at boot;
this requires linger. On agent-server, `osso` has `Linger=yes`; `allison` linger
was enabled with user approval on 2026-10-25. The server reuses `launch.json`:
Pi's `--session-id UUID` opens an existing conversation in the project, while
explicit `--resume UUID` uses `--session UUID`. Conversation, name, project and
account return; running jobs, an in-progress model turn and unsent editor text
do not. Whole-unit SIGKILL followed by a manual unit start proved conversation
restoration, not an actual host reboot. UUID-conflict handling is out of scope;
explicit resume semantics are unchanged.

Retained output exists only in the running server's bounded memory. `logs`
authenticates a one-shot local QUIC snapshot request; it does not attach or
resize the PTY and fails for stopped sessions. There is no disk history store.
History and replay-journal budgets are each 16 MiB by default and at most;
`--history-bytes` can reduce them. Fresh attachment resets outer-terminal
scrollback. Alternate-screen applications and unknown VT callbacks are unsupported;
SGR attributes the parser does not model (blink, conceal) are ignored, and
strikethrough is rendered.
See [implementation and proof limits](docs/wiki/systems/unified-pi-session.md)
and the single [Cargo dependency inventory](docs/wiki/systems/unified-pi-session.md#cargo-dependency-inventory).

### CI proof

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
terminal repaint, or installed deployment. GitHub run `40206416647` passed the macOS job, including native clipboard,
PTY and unit tests. Ubuntu failed a fixture readiness race, fixed in `81021f2`.
Run `40226091616` passed both jobs at `fdaa0d6`. Installed macOS-to-Linux
attachment passed at `7831e16` on an arm64 macOS 26.6.2 laptop; see the
[proof matrix](docs/wiki/systems/unified-pi-session.md#evidence-and-remaining-acceptance).

## Pi session switcher

The local `agent-switcher` provides searchable Pi sessions from the laptop's
Pi control database, the configured `agent-server` account, `allison`'s
agent-server sessions (shown as `allison@agent-server`, opened with
`--user allison`), and the `aso` account (opened with `--host aso`), through its
registered installed `remote-agent` client.

```text
./deploy.sh --switcher
agent-switcher
agent-switcher --list
agent-switcher --pick
agent-switcher --open KEY
```

Deployment is local-only; it does not replace the remote backend, server, or
protocol. The selector requires WezTerm and fzf; window focus uses Niri.
Default invocation opens a WezTerm window with fzf, starting WezTerm when it is not
running; `--pick` uses the current terminal,
`--list` emits JSON, and `--open` revalidates a selection key before acting.
Session IDs listed one per line in `~/.config/agent-switcher/pinned` appear at
the top, in file order. Named remote sessions follow, alphabetically, above the
local sessions.
Remote rows are cyan; local rows use the default foreground, with host/state labels
visible in both groups.
The prompt and initial list start at the top; typing retains normal search ranking.
Esc cancels. Errors must remain visible. Ctrl+A archives the highlighted stopped
local session; Ctrl+D deletes it after a `[Y/n]` confirmation. Archive sets Pi's
archive flag in place, and delete removes the transcript tree and control rows.
Both revalidate the selection and refuse running, unknown, and remote sessions.
Ctrl+R renames the highlighted session; a remote one runs `remote-agent rename
NAME NEW_NAME`, which restarts a started session under the new name with the
same conversation. Ctrl+S stops the highlighted session after a `[Y/n]` confirmation:
local Pi gets SIGTERM; remote sessions run `remote-agent stop NAME`.
The desktop binding contract is Super+A
for this selector, Super+S unchanged for Noctalia Session Menu, and removal of
the Settings shortcut.

Each session uses one WezTerm tab. Selecting a session whose Pi process or
`remote-agent attach` client runs in a WezTerm pane activates that pane, even in a
hidden tab, and focuses its window; the pane is identified by its PTY, not its
title. Otherwise the session opens in one new tab: running remote sessions through
installed remote-agent attachment to the same runtime, stopped local sessions with
`pi --session SESSION_PATH` in their saved directory. Sessions open outside
WezTerm (for example Ghostty), ambiguous attachments and multiple WezTerm windows
fail explicitly without starting anything. When the target tab is in the picker's
own window, that window is already focused and Niri is not consulted. `--open` outside WezTerm needs
`WEZTERM_UNIX_SOCKET` set to the GUI socket.
If Pi exited but a started remote session's server is still running, selecting
it resumes the same saved conversation before attaching. The switcher must also
resume an explicitly selected named saved remote session when its backend server
is absent, whether the session is marked started or stopped and unmarked. This
absent-server behavior is a requirement, not yet verified. In both cases, the
switcher must revalidate the selected host, account, name, cwd and saved
conversation, use the existing `restart NAME` command, verify the same saved
identity is running, and attach normally. It must refuse invalid or conflicting
states and ambiguous identities or clients. Explicit selection is not a sweep:
restart and deploy sweeps remain limited to sessions marked started. Unknown or
stale selections fail without fallback.
Discovery makes no provider calls, scans no transcripts, and does not discover
other accounts.

See the [session-switcher contract](docs/specs/session-switcher.md) for verified
behavior, test evidence, and remaining desktop acceptance limits.

## Provider login

Authenticate providers independently on each host; do not copy stored credentials
between hosts. `remote-agent login claude --host HOST --user USER`
starts the selected user's `~/.local/bin/claude auth login --claudeai` over SSH.
It retains an explicit `CLAUDE_CONFIG_DIR`; otherwise Claude uses the target
home's `.claude` directory. `remote-agent login claude ACCOUNT` instead logs in
the existing bridge profile `~/.config/pi-claude-bridge/profiles/ACCOUNT` by
setting it as `CLAUDE_CONFIG_DIR`; a missing profile or invalid name fails
without creating anything, and other providers reject ACCOUNT. The executable may
be an SDK symlink; a missing executable fails explicitly. Other providers use
`remote-agent login PROVIDER --host HOST --user USER`, which runs Pi login.
Both selectors are optional. Claude bridge authentication uses native Claude
credentials, not Pi's Anthropic login store; this command does not import Pi
grants or create or select bridge profiles. Existing sessions are unchanged.
Controlled executable tests cover login routing, credential-store selection,
real-TTY input/output and cancellation; they do not establish OAuth network success.

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
cargo test --locked
./run-tests.sh
./run-tests.sh test_deploy.py
```

Native scheduler and dispatch tests belong to Pi; see the
[canonical scheduler spec](https://github.com/Osso/pi/blob/master/docs/specs/periodic-checks.md#tests-asserting-this-spec).
Python discovery retains `tests/test_periodic_runtime.py` with
`tests/fixtures/periodic-runtime-provider.ts` as installed-Pi integration
acceptance. Set `PI_TEST_BINARY` to select the installed Pi under test.

The native attach, network, resize and upgrade tests (`tests/test_*.py`)
run `target/debug/remote-agent` (or `REMOTE_AGENT_BIN`) against real Pi
TUI code. They need Linux, Node 22.18+, `PI_TUI_SOURCE` pointing to Pi's
`packages/tui/src` and `@xterm/headless` on `NODE_PATH`; missing dependencies are
reported as skipped. Evidence stays under `target/native-tests/`.

## License

[MIT](LICENSE).

## Scope and documentation limits

The [configuration spec](docs/specs/configuration.md) tracks mapping behavior and
its tests. Host-specific provisioning and transport readiness must be established
for each installation; examples here are not evidence of a provisioned host.

# Pi session switcher

`agent-switcher` provides a searchable terminal selector for Pi sessions on the
local laptop and managed sessions on `agent-server`. Its Python standard-library
source lives in `switcher/`. This is a local client feature, not a replacement
for the installed remote-agent backend or protocol.

## What it must do

### Inventory and command surface

- [x] `agent-switcher --list` emits JSON inventory with selection keys, meaningful
  session names, host, and state. Local inventory reads Pi's control SQLite
  database read-only; remote inventory invokes the registered installed
  `remote-agent --host agent-server list`, not a repository build artifact.
- [x] Initial display places named remote sessions before local sessions, preserving alphabetical ordering within each group. Render the initial list from the top; typed searches retain normal relevance ranking and selection identities.
- [x] Session IDs listed one per line in `$XDG_CONFIG_HOME/agent-switcher/pinned`
  (default `~/.config`) appear above all other rows, in file order; a missing file
  pins nothing. This keeps the machine assistant at the top.
- [x] Remote rows use a distinct cyan terminal color while local rows retain the default foreground. Keep host/state labels visible, sanitize user-supplied controls before coloring, and preserve selection identity through fzf's ANSI-stripped output.
- [x] Default invocation opens a WezTerm window with an fzf selector through
  `wezterm start`, reusing the running WezTerm GUI. `--pick` runs the selector in
  the current terminal; `--open KEY` acts on one selected entry.
- [x] Search covers local and remote entries. Esc cancels without opening a
  session; failures remain visible instead of closing an unreadable terminal.
  Fatal chooser errors wait for Enter before exit 1; normal cancellation exits 0.
- [x] Before any action, re-read the selected entry. Unknown or stale selections
  fail explicitly without fallback.
- [ ] Ambiguous selections fail explicitly without fallback.

### Open and resume

Each session lives in one WezTerm tab. A session's terminal is the `/dev/pts`
device on stdin of its process: the bound local Pi PID, or every local process
the kernel names `remote-agent` (`/proc/PID/comm`: the binary, or the script under
an interpreter) whose parsed arguments are `attach NAME` for the session's host
(global `--host` in any position or `--host=`; absent means `agent-server`) and no
`--user`. Several processes on one terminal are one client. WezTerm panes are listed with `wezterm cli --no-auto-start list`
and matched by `tty_name`; titles are never used for identity.

- [x] A running session whose terminal is a WezTerm pane, including a hidden tab,
  is selected with `wezterm cli activate-pane`, then its window is focused through
  Niri. No process is started or attached.
- [x] A session with no client opens in one new tab (`wezterm cli spawn`) of the
  lowest-numbered WezTerm window other than the picker's, or a new window when
  none exists: running remote sessions run installed `remote-agent --host HOST
  attach NAME`; stopped local sessions run `pi --session SESSION_PATH` in their
  saved directory.
- [x] Each activated or spawned tab is titled with the session name the picker
  shows (`wezterm cli set-tab-title`), for example `Assistant` or `wow-ui-sim`;
  the WezTerm config shows an explicit tab title as-is.
- [x] A session open outside WezTerm (for example a Ghostty tab) reports its
  terminal and starts nothing. A running local Pi or remote client without a live
  terminal reports already-open. Clients on two terminals report ambiguity.
- [x] Niri focus targets the only `org.wezfurlong.wezterm` window that is not a
  picker window, recognized by the `Agent sessions` title the picker sets; the
  picker may be unfocused. Two or more candidates fail before any activation or
  spawn instead of guessing; WezTerm X11 windows expose no window ID that Niri reports.
  When the target is the picker's own window (`--pick` run inside a session window),
  it is already focused and Niri is not consulted.
- [ ] For a stopped remote saved name, report explicit manual resume/new-name
  instructions. Do not start under the same existing session directory, invent
  UUID handling, or automatically rename the session.

### Desktop integration and deployment

- [ ] Super+A launches `agent-switcher`. Super+S remains the Noctalia Session Menu;
  remove the Settings shortcut without replacing the Session Menu binding.
- [x] `./deploy.sh --switcher` installs the local switcher only. It does not
  replace backend/server/protocol components or restart existing sessions.
- [x] Discovery and selection make no provider calls, scan no transcript
  filesystem, and perform no cross-account discovery.

## How it works

- [Desktop and command usage](../../README.md#pi-session-switcher)
- [Existing remote launch policy](configuration.md#rust-account-and-launch-policy)

## Implementation inventory

These paths identify the implementation scope; they do not assert acceptance.

- `agent-switcher`: local executable entry point.
- `switcher/inventory.py`: read-only local control-database and installed remote inventory.
- `switcher/picker.py`: searchable terminal selection.
- `switcher/main.py`: command dispatch and visible error handling.
- `switcher/actions.py`: selection revalidation, local focus/resume, and remote attachment.
- `deploy.sh` and `deploy.py`: local-only `--switcher` deployment path.

## Tests asserting this spec

Checked requirements have passing backing tests at `6f39ebd`. Fixture acceptance
is not live desktop acceptance.

- `tests/test_switcher_inventory.py` (26): read-only inventory sources, process
  identity, invalid data, partial failures, bounded retries, and remote-first ordering.
- `tests/test_switcher_picker.py` (14): names/host/state display, selection identity,
  cancellation, missing/broken fzf, invalid selections, inventory errors, top-down
  layout, remote color, and selection through native fzf's ANSI-stripped output.
- `tests/test_switcher_main.py` (23): combined inventory without transcript changes,
  stale selections, stopped-remote refusal, and WezTerm actions with real PTY child
  processes and executable `wezterm`/`niri` fixtures: hidden-tab activation for local
  and remote, one new tab for local resume and remote attach, new window when none
  exists, picker window excluded, outside-WezTerm/ambiguous/terminal-less refusal,
  attach-argument variants, multiple-window refusal, malformed pane inventory, and
  the `wezterm start` launcher argv.
  `test_fatal_inventory_error_stays_visible_until_enter` uses a real PTY to assert
  the error, live dismissal wait, Enter, and exit 1.
- `tests/test_switcher_deploy.py` (3): repeatable local-only installation, unchanged
  maps/backend executable, and unmanaged-file/foreign-symlink refusal.

### Proof ledger

Existing evidence was inspected, not rerun for this documentation update.
Commands below cover `43f4e93b1ca909850a17fefc106beaac94a1e795`;
`/tmp/pi-switcher/FINAL-VERIFIER-2.md` records full results and applicability.

| Command | Result | Evidence under `/tmp/pi-switcher/` |
| --- | --- | --- |
| `./run-tests.sh unit 'test_switcher_*.py'` | 50/50 pass | `followup-feature-tests.log` |
| `ruff check agent-switcher switcher/ tests/test_switcher_inventory.py tests/test_switcher_picker.py tests/test_switcher_main.py tests/test_switcher_deploy.py` | Clean | `followup-ruff-check.log` |
| `ruff format --check agent-switcher switcher/ tests/test_switcher_inventory.py tests/test_switcher_picker.py tests/test_switcher_main.py tests/test_switcher_deploy.py deploy.py` | 11 files formatted | `followup-ruff-format.log` |

Retained `tests/test_rust_deploy.py` proof is 8/8 Python deployment fixtures in
`final-scoped-suite.log`, not Rust compilation. The verifier confirms those
files and deployment paths are unchanged since the prior `39f765f` verification.

Stored installed-command smoke artifacts do not embed a source revision; the
verifier establishes applicability to unchanged successful picker/action paths:

- `picker-smoke.json` and `screen-search.txt`: real fzf in an isolated tmux PTY;
  search `world-of-osso` displays an `agent-server` running row; Escape exits 0
  without selection.
- `remote-attach-smoke.json`: installed `agent-switcher --open KEY` uses native
  SSH/QUIC attachment and Ctrl+backslash detach, exits 0, and preserves conversation
  and server/child process identities without submitting a prompt.
- `desktop-launch-smoke.json`: default installed invocation opened actual Ghostty
  (superseded by the WezTerm launcher at `33a94ff`).
  The harness waited synchronously for the GUI, then backgrounded; its own job
  was canceled and only verified owned process groups were terminated. All owned
  test processes exited. This is launch proof, not normal desktop-close proof.
- Provisioning `350fce8526c19d1d72ab13f5b9cbc90e49aa34d4`: source bindings preserve
  Super+A/S as specified and `niri validate` passed (`niri-validate.log`, with
  unchanged-config applicability in the verifier). No physical keys were injected.

Outside this feature, `test_deploy_users.py` has two failing subtests in
`test_unconfigured_deploy_keeps_default_behavior_and_leaves_other_worker_stale`
(`setting=None` and `setting=[]`), both expecting a controller symlink at line 290.
`baseline-deploy-users.log` reproduces them identically on exact pre-feature
`f4df7b895619fa9bbc1606cb4e352a5aa6c2c515`; baseline integrity is recorded in
`followup-baseline-integrity.json`. The unchanged `deploy.py:262` S102 `exec`
warning is also outside feature scope. Neither is fixed or suppressed here.

### Remote priority and color proof

At `e855767`, `./run-tests.sh unit 'test_switcher_*.py'` passes all 54 tests,
including the native fzf ANSI roundtrip. Feature Ruff checks and formatting pass;
`/tmp/pi-switcher/priority-color-VERIFIED.md` records independent results.

The installed picker was also rendered in a private real tmux PTY. The prompt is
at row 0, remote entries occupy rows 4–8, and local entries begin at row 9. All
five rendered remote rows carry cyan foreground. Search still finds the remote
match and Escape exits 0 without selecting or starting a session. Evidence:
`remote-first-color-smoke.json`, `remote-first-screen.txt`, and
`remote-first-screen.ansi` under `/tmp/pi-switcher/`. These observations establish
terminal rendering, not physical global-key acceptance.

### WezTerm tab selection proof

At `6f39ebd`, `./run-tests.sh unit 'test_switcher_*.py'` passes 66/66; Ruff check and
format of switcher files are clean.

Installed live run at `6f39ebd` against the existing WezTerm GUI, driving the real
default launcher and fzf (`/tmp/pi-wezterm-switcher/live_accept.py`, result
`live-accept-2.json`; the main window also held a zsh tab and the Assistant tab):

| Step | Observation |
| --- | --- |
| Before | `clank` backend PID/child PID `1164956`/`1164957`, conversation `50a99d03…`, running |
| Launch | `agent-switcher` opened the picker; `wezterm start` reported spawning via the existing GUI instance |
| First select `clank` | One attach client (`comm` `remote-agent`) in a new tab of the main window, 0.42 s after Enter; Niri focused WezTerm; picker closed |
| Hide | Main window title `[1/3] zsh` |
| Second select `clank` | Title `[3/3] π - Clank sandbox - wowdecomp` 0.279 s after Enter; same client PID, no new attach |
| Detach (Ctrl+\\) | Client exited, tab closed; backend PID/child PID/conversation unchanged |

Local live run at `1e0eb3c` (`live_local.py`, result `live-local.json`):

| Step | Observation |
| --- | --- |
| Hidden running local Pi | Assistant tab hidden (`[1/2] zsh`); selecting `Assistant` showed `[2/2] π - Assistant - osso` 0.134 s after Enter; same PID 1481183, pane count unchanged |
| Stopped local resume | A disposable one-message session opened in exactly one new tab; its `pi` process ran on that pane's PTY in the saved cwd and the same session file, running 1.865 s after Enter; `/quit` closed the tab |
| Cleanup | The disposable session was removed with Pi's native `deleteSessionTree` (trash); metadata and picker row gone |

Active tab is read from the window title: `wezterm cli list-clients`
`focused_pane_id` did not follow `activate-pane` and is not evidence.
`live-refusal.txt`: selecting `clank` while attached in Ghostty displayed
`Session is open outside WezTerm on /dev/pts/7` and started nothing.

Live runs found three defects, each fixed with a failing test first: exact-argv
matching missed `attach NAME --host H` and created a duplicate client (`a076ddd`);
an unfocused picker counted as a second WezTerm window (`33a1b81`); the transient
`wezterm cli spawn -- remote-agent ...` process counted as a client (`8117cef`).
Verifier findings fixed: multi-window refusal spawned first (`751be3d`); any program
given `remote-agent` as argv[1] counted as a client, a terminal-less remote client
allowed a duplicate tab, and a wrapper with its child on one PTY reported ambiguity
(`6f39ebd`).

## Known gaps (current cycle)

- [ ] Physical Super+A launch and Super+S Session Menu acceptance remain unverified;
  source binding review and Niri validation do not test physical shortcuts.
- [ ] Multiple-window refusal has fixtures only. Physical Super+A keypresses were not
  injected; live runs executed the same installed `agent-switcher` command.
- [ ] Stopped-remote refusal is tested, but its fixture asserts only nonzero exit,
  a stopped message, and no replacement launch. Exact manual resume/new-name
  instructions lack a backing assertion, so that full requirement stays unchecked.

## Out of scope

- Ghostty tab selection: Ghostty 1.3.1 exposes no tab list or focus API. Sessions
  open in Ghostty are refused, not switched; detach them there and select them again
  to move them into WezTerm.
- Automatic stopped-remote restart: an existing saved-name directory cannot be
  reused by start; manual resume with a new name remains explicit.
- Cross-account discovery, transcript scanning, and provider requests: inventory
  is restricted to the local control database and configured remote account.
- Backend, server, and protocol replacement: deployment is local-only and must
  preserve existing remote sessions.

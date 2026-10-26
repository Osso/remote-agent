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
- [ ] Initial display places named remote sessions before local sessions, preserving alphabetical ordering within each group. Render the initial list from the top; typed searches retain normal relevance ranking and selection identities.
- [x] Default invocation opens Ghostty with an fzf selector. `--pick` runs the
  selector in the current terminal; `--open KEY` acts on one selected entry.
- [x] Search covers local and remote entries. Esc cancels without opening a
  session; failures remain visible instead of closing an unreadable terminal.
  Fatal chooser errors wait for Enter before exit 1; normal cancellation exits 0.
- [x] Before any action, re-read the selected entry. Unknown or stale selections
  fail explicitly without fallback.
- [ ] Ambiguous selections fail explicitly without fallback.

### Open and resume

- [x] Focus a running local Pi through Niri when a visible Ghostty window matches
  its exact native Pi title and process ancestry; reject a wrong process match.
- [ ] Reject duplicate matching windows rather than choosing one.
- [x] If a running local Pi has no identifiable visible Ghostty window, report
  already-open/unavailable. Never start a duplicate Pi to bypass the missing mapping.
- [ ] Apply the same refusal to arbitrary hidden tabs and non-unique window matches;
  the missing-visible-window fixture is not a real hidden-tab acceptance test.
- [x] Attach a running remote managed session through the installed remote-agent
  attach command, preserving that session's existing runtime.
- [x] Resume a stopped local session with native `pi --session SESSION_PATH` in
  its saved working directory.
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

Checked requirements have passing backing tests at `43f4e93`. Fixture acceptance
is not live desktop acceptance.

- `tests/test_switcher_inventory.py` (24): read-only inventory sources, process
  identity, invalid data, partial failures, and bounded retries.
- `tests/test_switcher_picker.py` (11): names/host/state display, selection identity,
  cancellation, missing/broken fzf, invalid selections, and inventory errors.
- `tests/test_switcher_main.py` (12): combined inventory without transcript changes,
  native local resume argv/cwd, remote attach without start/resume, stale selections,
  exact-title/ancestry focus, missing-window refusal, and desktop launcher argv.
  `test_fatal_inventory_error_stays_visible_until_enter` uses a real PTY to assert
  the error, live dismissal wait, Enter, and exit 1. Other fatal causes have
  picker fixtures, not separate end-to-end PTY acceptance.
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
- `desktop-launch-smoke.json`: default installed invocation opens actual Ghostty.
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

## Known gaps (current cycle)

- [ ] Physical Super+A launch and Super+S Session Menu acceptance remain unverified;
  source binding review and Niri validation do not test physical shortcuts.
- [ ] Normal desktop close remains unverified; the Ghostty launch smoke required
  owned-process cleanup rather than a physical close-key acceptance test.
- [ ] Local native Pi focus/resume and hidden-tab refusal have fixtures only, not
  live user-session acceptance. Duplicate-window rejection and unknown-remote
  actions also lack live acceptance. Duplicate matching-window refusal and
  ambiguous-selection refusal lack a mapped passing assertion in this ledger.
- [ ] Stopped-remote refusal is tested, but its fixture asserts only nonzero exit,
  a stopped message, and no replacement launch. Exact manual resume/new-name
  instructions lack a backing assertion, so that full requirement stays unchecked.

## Out of scope

- Hidden Ghostty tab activation: an arbitrary hidden tab has no reliable window
  mapping in this contract; report unavailable rather than duplicate it.
- Automatic stopped-remote restart: an existing saved-name directory cannot be
  reused by start; manual resume with a new name remains explicit.
- Cross-account discovery, transcript scanning, and provider requests: inventory
  is restricted to the local control database and configured remote account.
- Backend, server, and protocol replacement: deployment is local-only and must
  preserve existing remote sessions.

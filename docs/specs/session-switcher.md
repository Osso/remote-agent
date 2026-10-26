# Pi session switcher

`agent-switcher` provides a searchable terminal selector for Pi sessions on the
local laptop and managed sessions on `agent-server`. Its Python standard-library
source lives in `switcher/`. This is a local client feature, not a replacement
for the installed remote-agent backend or protocol.

## What it must do

### Inventory and command surface

- [ ] `agent-switcher --list` emits JSON inventory with selection keys, meaningful
  session names, host, and state. Local inventory reads Pi's control SQLite
  database read-only; remote inventory invokes the registered installed
  `remote-agent --host agent-server list`, not a repository build artifact.
- [ ] Default invocation opens Ghostty with an fzf selector. `--pick` runs the
  selector in the current terminal; `--open KEY` acts on one selected entry.
- [ ] Search covers local and remote entries. Esc cancels without opening a
  session; failures remain visible instead of closing an unreadable terminal.
- [ ] Before any action, re-read the selected entry. Unknown, stale, or ambiguous
  selections fail explicitly without fallback.

### Open and resume

- [ ] Focus a running local Pi only when one visible Ghostty window is identified
  by its exact native Pi title, process ancestry, and uniqueness; focus uses Niri.
- [ ] If a running local Pi is in an arbitrary hidden Ghostty tab, or its window
  cannot be identified uniquely, report already-open/unavailable. Never start a
  duplicate Pi to bypass the missing mapping.
- [ ] Attach a running remote managed session through the installed remote-agent
  attach command, preserving that session's existing runtime.
- [ ] Resume a stopped local session with native `pi --session SESSION_PATH` in
  its saved working directory.
- [ ] For a stopped remote saved name, report explicit manual resume/new-name
  instructions. Do not start under the same existing session directory, invent
  UUID handling, or automatically rename the session.

### Desktop integration and deployment

- [ ] Super+A launches `agent-switcher`. Super+S remains the Noctalia Session Menu;
  remove the Settings shortcut without replacing the Session Menu binding.
- [ ] `./deploy.sh --switcher` installs the local switcher only. It does not
  replace backend/server/protocol components or restart existing sessions.
- [ ] Discovery and selection make no provider calls, scan no transcript
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

The integration owner must supply results and confirm the assertions in these
planned test files. No passing result is claimed here.

- `tests/test_switcher_inventory.py`: inventory sources, identity, and invalid data.
- `tests/test_switcher_picker.py`: searchable selection and cancellation.
- `tests/test_switcher_main.py`: commands, actions, stale selections, and visible errors.
- `tests/test_switcher_deploy.py`: local-only installation boundaries.

## Known gaps (current cycle)

- [ ] Supply test evidence before marking any requirement verified.
- [ ] Establish installed Ghostty/Niri focus, Super+A, and cancellation behavior;
  fixture results alone do not prove desktop integration.
- [ ] Establish that installed remote attachment preserves the selected runtime;
  this documentation update does not exercise attachment.

## Out of scope

- Hidden Ghostty tab activation: an arbitrary hidden tab has no reliable window
  mapping in this contract; report unavailable rather than duplicate it.
- Automatic stopped-remote restart: an existing saved-name directory cannot be
  reused by start; manual resume with a new name remains explicit.
- Cross-account discovery, transcript scanning, and provider requests: inventory
  is restricted to the local control database and configured remote account.
- Backend, server, and protocol replacement: deployment is local-only and must
  preserve existing remote sessions.

# Cross-user attachment

Administrators can discover and attach another Unix account's managed sessions
through `remote-agent list --user USER` and `remote-agent attach NAME --user USER`.
The controller and privileged helper implement the account boundary; see
[another user's sessions](../../README.md#another-users-sessions) for setup.

## What it must do

- [x] Only `list` and `attach` accept the validated user selector; omission preserves
  same-account behavior. Covered by `CrossUserControllerTests` and existing controller tests.
- [x] Resolve the target home from the server account directory and invoke the
  target worker through the SSH account's existing `authsudo` authorization.
  No authorization grants, mapping changes or credential copying occur.
  Covered by the account-directory/process fixtures in `CrossUserControllerTests`.
- [x] Use the target HOME, USER, LOGNAME, runtime and Pi config directory, without
  inheriting administrator credentials or tmux socket context. Covered by
  `test_list_user_reads_target_mapping_and_clears_admin_environment`.
- [x] Listing labels target-user results; Mosh reconnect and clean-detach status
  checks remain in the same account. Covered by `CrossUserControllerTests`.
- [x] Unknown users, authorization failures and wrapped hosts fail explicitly;
  authorization/configuration failure does not enter Mosh retries. Covered by
  `CrossUserControllerTests`.
- [x] Actual Unix UID, default tmux socket ownership, and interactive Mosh
  attachment are proven with same-named owned disposable sessions across two
  server accounts; clean detach preserves both sessions. Proven by live acceptance.

## How it works

- [Another user's sessions](../../README.md#another-users-sessions).

## Implementation inventory

- `remote-agent.py` — CLI selection, listing and attachment transport.
- `remote-agent-user.py` — account lookup, clean target environment and authorized execution.
- `deploy.py` — helper and behavioral-test payload installation.

## Tests asserting this spec

- `tests/test_cross_user.py` — real process boundaries with account/authorization fixtures.
- `tests/test_remote_agent.py` — existing same-account controller and terminal behavior.
- Live acceptance uses owned disposable tmux sessions; no real research session
  is read, typed into, restarted or terminated.

## Known gaps (current cycle)

None.

## Out of scope

- Arbitrary standalone Pi or unmanaged tmux sessions.
- Cross-user launch, cancellation, provider login, deployment or configuration commands.
- Account provisioning, privilege grants, SSH key changes or shared control databases.
- Wrapped/Windows transport and messaging relay changes.

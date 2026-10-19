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
- Original live acceptance used owned disposable tmux sessions, proving
  same-named account isolation and clean detach; that trial did not touch real
  research sessions.
- On 2026-10-19, one user-authorized actual research Pi session was attached
  through cross-user SSH/Mosh/dtach at 48×200 and cleanly detached with exit 0.
  Process and health metadata confirmed the target cwd and selected conversation.
  The existing tmux client and unrelated research process identities were preserved.
  This is bounded operational acceptance, not exhaustive cross-user dtach isolation
  or full-history pixel proof.

## Known gaps (current cycle)

- [ ] Exhaustive cross-user dtach isolation matrix and full-history pixel proof remain unproven. The single actual attachment above and earlier disposable tmux isolation trial provide distinct, bounded evidence; see [managed session backends](session-backends.md).

## Out of scope

- Arbitrary standalone Pi or unmanaged tmux sessions.
- Cross-user launch, cancellation, provider login, deployment or configuration commands.
- Account provisioning, privilege grants, SSH key changes or shared control databases.
- Wrapped/Windows transport and messaging relay changes.

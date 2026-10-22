# Isolated clank Pi

An agent-server container runs OpenRouter Pi and the reviewed `td` client. Host wrappers integrate with remote-agent; [operation and isolation](../../sandbox/README.md) describes deployment.

## What it must do

- [ ] Run Pi plus clank `td` as an unprivileged container user with a read-only root filesystem, only dedicated OpenRouter credentials and model `deepseek/deepseek-v4.1-flash-1017`.
- [ ] Keep host homes, credentials, SSH agents and container-management sockets inaccessible; persist only dedicated workspace and Pi state.
- [ ] Deny container-initiated host, LAN, private, metadata and IPv6 traffic while allowing public IPv4 internet.
- [ ] Install reviewed td read-only with self-update disabled; do not authenticate clank or install it on the host.
- [ ] Preserve normal remote-agent start, resume, SSH history export, Mosh attachment/detach and scoped cancellation; cancellation must terminate only the selected conversation's container.
- [ ] Load host isolation before starting any session, including after host reboot.

## How it works

- [Deployment and trust boundaries](../../sandbox/README.md).

## Implementation inventory

- `sandbox/firewall.py` — dedicated host nftables policy.
- `sandbox/Dockerfile` — unprivileged immutable runtime image.
- `sandbox/launch.py` — credential environment and Pi selection.
- `sandbox/session.py` — host-side per-conversation lifecycle, selected cancellation and SIGWINCH proxy to the running container.
- `sandbox/deploy.sh`, `sandbox/deploy.py` — supported deployment entry point and source/artifact staging.
- `sandbox/host_deploy.py` — host firewall/service, image, volume and wrapper installation.
- `sandbox/runtime_update.py` — launcher-only image update preserving audited td/Pi bytes.
- `remote-agent-worker.py` — project-specific launch and cancellation executables.

## Tests asserting this spec

- `tests/test_sandbox_firewall.py` — serialized firewall policy and validation.
- `tests/test_sandbox_launch.py` — credential environment and fixed provider/model arguments.
- `tests/test_sandbox_session.py` — per-conversation container routing and selected cancellation.
- `tests/test_remote_agent.py`, `tests/test_history.py` — worker lifecycle routing and history integration.
- Source-test agents reported targeted passes; independent final verification remains pending. Test paths alone do not establish completion.
- [Verified 2026-10-22 coverage and installed inventory](../../sandbox/README.md#verification) records concrete network/model/resume/repaint artifacts and main's scoped-cancellation observations. Source requirements remain unchecked until backing proof is reconciled by the independent gate.

## Known gaps (current cycle)

- [ ] Independent final gate: reconcile source-test results and installed/live evidence before final GO; deployment and live network/model/SSH-history/Mosh/repaint/cancellation observations are now recorded, not pending execution.
- [ ] Full host reboot acceptance: service enabled and `Before=docker.service` established, but no actual host reboot performed. Proof-B conversation restart is not host-reboot proof.

## Out of scope

- Clank account/token setup, server operation, local compiler/Ghidra installation and host td installation.
- Eliminating supply-chain, container/runtime or shared-kernel exploit risk. No observed malware does not establish safety; the unsafe updater is disabled, not trusted.
- Preventing sandbox code from reading/exfiltrating its own OpenRouter key: exposure is unavoidable with code execution and public internet. Writable volumes can execute other downloaded code.

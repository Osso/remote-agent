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
- `sandbox/session.py` — host-side per-conversation container lifecycle.
- `sandbox/deploy.sh` — source/artifact staging and supported deployment (in flight).
- `remote-agent-worker.py` — project-specific launch and cancellation executables.

## Tests asserting this spec

- `tests/test_sandbox_firewall.py` — serialized firewall policy and validation.
- `tests/test_sandbox_launch.py` — credential environment and fixed provider/model arguments.
- `tests/test_sandbox_session.py` — per-conversation container routing and selected cancellation.
- Exact-revision passing results and installed network/real SSH/Mosh acceptance proofs are pending main-session evidence; test paths do not establish completion.

## Known gaps (current cycle)

- [ ] Per-conversation wrapper/cancel callback integration and exact-revision test evidence pending.
- [ ] Live deployment and isolation/attachment evidence pending.

## Out of scope

- Clank account/token setup, server operation, local compiler/Ghidra installation and host td installation.
- Eliminating supply-chain, container/runtime or shared-kernel exploit risk. No observed malware does not establish safety; the unsafe updater is disabled, not trusted.
- Preventing sandbox code from reading/exfiltrating its own OpenRouter key: exposure is unavoidable with code execution and public internet. Writable volumes can execute other downloaded code.

# Writable OpenRouter VM

The writable VM in `sandbox/openrouter-vm/` replaces the `wowdecomp` container without changing the host's Rust remote-agent transport. See [operation](../../sandbox/openrouter-vm/README.md).

## What it must do

- [ ] Run a real KVM guest with its own kernel, writable persistent disk, four virtual CPUs, 8 GiB RAM, and a 64 GiB sparse disk; verify the pinned official Arch image's digest and signature before boot.
- [ ] Provide key-only SSH administration and an unprivileged `agent` account with UID 1000. Do not share host directories, credentials, sockets, or an SSH agent with the guest.
- [ ] Allow only the fixed OpenRouter proxy and the fixed Arch package mirror. Keep arbitrary public, LAN, host-service, metadata, DNS, and IPv6 access blocked, including for guest root. Keep package signature checks enabled.
- [ ] Permit persistent package installation without rebuilding the VM image. Provide Capstone, Unicorn, Java, Ghidra, and Wine without launching WoW.
- [ ] Transfer logs before session cutover; preserve the original workspace, executable provenance, Pi home, conversation UUID, model settings, and history. Keep old data until migration acceptance and do not cancel the old session as a shortcut.
- [ ] Attach through the installed Rust remote-agent using SSH control and QUIC; preserve terminal input, size changes, exit status, and clean detach without stopping Pi. Prevent two guest Pi processes from opening the same conversation concurrently.

## How it works

- [VM deployment and migration](../../sandbox/openrouter-vm/README.md).
- [Existing container and tool-install proof](../../sandbox/openrouter-runtime/README.md).

## Implementation inventory

- `sandbox/openrouter-vm/deploy.sh`, `deploy.py`, `host_deploy.py` — staging, signed image provisioning, cloud-init and the user service.
- `sandbox/openrouter-vm/vm.py` — KVM process, disks and explicitly restricted network forwards.
- `sandbox/openrouter-vm/relay.py` — byte stream to the existing fixed OpenRouter proxy.
- `sandbox/openrouter-vm/repo_proxy.py` — GET/HEAD access to the single HTTPS Arch mirror.
- `sandbox/openrouter-vm/session.py`, `guest_session.py` — host SSH launch and guest Pi environment, working directory and conversation lock.

## Tests asserting this spec

- `tests/test_openrouter_vm_deploy.py` — provisioning metadata, cloud-init, image verification and existing disk preservation.
- `tests/test_openrouter_vm_proxies.py` — concrete HTTP transfer, denied requests and bidirectional relay behavior.
- `tests/test_openrouter_vm_session.py` — Pi launch environment, identity and configuration preservation, process-lifetime conversation locking and SSH arguments.

## Known gaps (current cycle)

- [ ] Establish real guest boot, writable package installation and enforced network boundaries.
- [ ] Transfer logs and session data with integrity evidence, then prove installed terminal attachment, resize and detach for the same conversation.
- [ ] Verify restart lifetime: the host SSH process must not leave a duplicate guest Pi after restart.

## Out of scope

- WoW execution, live game connections and analysis-code changes.
- General internet access, arbitrary proxy targets, LAN access and shared host filesystems.
- Preservation of an in-progress model turn, unsent editor text or the old PTY process across conversation resume.

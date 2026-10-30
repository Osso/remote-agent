# Writable OpenRouter VM

The writable VM in `sandbox/openrouter-vm/` replaces the `wowdecomp` container without changing the host's Rust remote-agent transport. See [operation](../../sandbox/openrouter-vm/README.md).

## What it must do

- [ ] Run a real KVM guest with its own kernel, writable persistent disk, four virtual CPUs, 8 GiB RAM, and a 64 GiB sparse disk; verify the pinned official Arch image's digest and signature before boot.
- [ ] Provide key-only SSH administration and an unprivileged `agent` account with UID 1000. Do not share host directories, unrelated host credentials, management sockets, or an SSH agent with the guest.
- [ ] Allow only the fixed OpenRouter proxy and the fixed Arch package mirror. Keep arbitrary public, LAN, host-service, metadata, DNS, and IPv6 access blocked, including for guest root. Keep package signature checks enabled.
- [ ] Permit persistent package installation without rebuilding the VM image. Provide Capstone, Unicorn, Java, Ghidra, and Wine without launching WoW as an installation test.
- [ ] Install the shared container tools manifest, including the headless Wine software graphics packages, in the guest.
- [ ] Transfer logs before session cutover; preserve the original workspace, executable provenance, Pi home, conversation UUID, model settings, and history. Keep old data until migration acceptance and do not cancel the old session as a shortcut.
- [ ] Attach through the installed Rust remote-agent using SSH control and QUIC; preserve terminal input, size changes, exit status, and clean detach without stopping Pi. Prevent two guest Pi processes from opening the same conversation concurrently.

## How it works

- [VM deployment and migration](../../sandbox/openrouter-vm/README.md).
- [Existing container and tool-install proof](../../sandbox/openrouter-runtime/README.md).

## Implementation inventory

- `sandbox/openrouter-vm/deploy.sh`, `deploy.py`, `host_deploy.py` — staging, signed image provisioning, cloud-init and the user service.
- `sandbox/openrouter-vm/vm.py` — KVM process, disks and explicitly restricted network forwards.
- `sandbox/openrouter-vm/install_runtime.py` — immutable-image Pi installation, controlled package installation and launcher-only updates.
- `sandbox/openrouter-vm/relay.py` — byte stream to the existing fixed OpenRouter proxy.
- `sandbox/openrouter-vm/repo_proxy.py` — GET/HEAD access to the single HTTPS Arch mirror.
- `sandbox/openrouter-vm/session.py`, `guest_session.py` — host SSH launch and guest Pi environment, working directory and conversation lock.

## Tests asserting this spec

- `tests/test_openrouter_vm_deploy.py` — provisioning metadata, cloud-init, image verification and existing disk preservation.
- `tests/test_openrouter_vm_proxies.py` — concrete HTTP transfer, denied requests and bidirectional relay behavior.
- `tests/test_openrouter_vm_session.py` — Pi launch environment, identity and configuration preservation, process-lifetime conversation locking, read-only export arguments and SSH arguments.
- `tests/test_openrouter_vm_runtime.py`, `tests/test_openrouter_vm_signatures.py` — runtime deployment, transient download retries, launcher-only preservation and real signature acceptance/rejection.

## Live evidence and gaps — 2026-10-30

- [x] Booted the KVM guest and installed tools and headless packages on its writable disk. The pinned image digest and signature, and both cloud-init schemas, were verified.
- [x] Transferred 13 log files before cutover; all transferred files matched their SHA-256 manifest. The final home/workspace archive matched its transferred SHA-256 and was extracted. The old session was stopped without cancellation and its record and data were retained.
- [x] Resumed the same conversation and reattached through the installed Rust client using SSH control and QUIC. The live Pi was running Ghidra analysis.
- [x] A direct UID-1000 check used the existing Xvfb and a disposable Wine prefix: llvmpipe worked and a Notepad X11 window remained present for two seconds after creation. No game was launched as a test.
- [x] Deployed the export fix without restarting Pi; the entire pre-migration saved history text remains an exact prefix of the resumed history after removing SGR color sequences. A real installed attachment resized the guest TTY and detached with exit 0 while preserving Pi and the existing user's geometry.
- [ ] Verify restart lifetime without duplicate guest Pi, cancellation lifecycle, host reboot and guest updates. Sampled root network checks are not exhaustive isolation proof.

The exact evidence and limits are in the [migration record](../../sandbox/openrouter-vm/README.md#live-migration-evidence--2026-10-30).

## Out of scope

- WoW execution as an installation test, live game connections and changes to analysis code. The installation task does not prohibit the agent's separately authorized analysis.
- General internet access, arbitrary proxy targets, LAN access and shared host filesystems.
- Preservation of an in-progress model turn, unsent editor text or the old PTY process across conversation resume.

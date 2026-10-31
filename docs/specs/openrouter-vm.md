# Writable OpenRouter VM

The writable VM in `sandbox/openrouter-vm/` replaces the `wowdecomp` container without changing the host's Rust remote-agent transport. See [operation](../../sandbox/openrouter-vm/README.md).

## What it must do

- [ ] Run a real KVM guest with its own kernel, writable persistent disk, four virtual CPUs, 8 GiB RAM, and a 64 GiB sparse disk; verify the pinned official Arch image's digest and signature before boot.
- [ ] Provide key-only SSH administration and an `agent` account with UID 1000. Normal deployment must persist the guest-only sudo policy `agent ALL=(ALL:ALL) NOPASSWD: ALL`. Do not share host directories except the read-only WoW installation data specified below; do not share host homes, host keys, unrelated host credentials, management sockets, or an SSH agent with the guest.
- [ ] Expose only `/syncthing/World of Warcraft` as a read-only host data share at the same guest path. Use 9p tag `wow-install`, `security_model=mapped-xattr`, and `readonly=on`; persist the guest mount with `ro,nodev,nosuid,trans=virtio,version=9p2000.L,cache=none`. Do not require `noexec` or make the guest disk read-only.
- [ ] Allow only the fixed OpenRouter proxy and the fixed Arch package mirror. Keep arbitrary public, LAN, host-service, metadata, DNS, and IPv6 access blocked, including for guest root. Keep package signature checks enabled.
- [ ] Permit persistent package installation without rebuilding the VM image. The agent must be able to run `sudo -n /usr/local/bin/arch install <package>` through the controlled Arch mirror. Provide Capstone, Unicorn, Java, Ghidra, and Wine without launching WoW as an installation test.
- [ ] Install the shared container tools manifest, including the headless Wine software graphics packages, in the guest.
- [ ] Transfer logs before session cutover; preserve the original workspace, executable provenance, Pi home, conversation UUID, model settings, and history. Keep old data until migration acceptance and do not cancel the old session as a shortcut.
- [ ] Attach through the installed Rust remote-agent using SSH control and QUIC; preserve terminal input, size changes, exit status, and clean detach without stopping Pi. Prevent two guest Pi processes from opening the same conversation concurrently.

## How it works

- [VM deployment and migration](../../sandbox/openrouter-vm/README.md).
- [Standing restart authorization and recurring checks](../../sandbox/openrouter-vm/OPERATIONS.md).
- [Existing container and tool-install proof](../../sandbox/openrouter-runtime/README.md).

## Implementation inventory

- `sandbox/openrouter-vm/deploy.sh`, `deploy.py`, `host_deploy.py` — staging, signed image provisioning, cloud-init and the user service.
- `sandbox/openrouter-vm/vm.py` — KVM process, disks and explicitly restricted network forwards.
- `sandbox/openrouter-vm/install_runtime.py` — immutable-image Pi installation, controlled package installation and launcher-only updates.
- `sandbox/openrouter-vm/relay.py` — byte stream to the existing fixed OpenRouter proxy.
- `sandbox/openrouter-vm/repo_proxy.py` — GET/HEAD access to the single HTTPS Arch mirror.
- `sandbox/openrouter-vm/session.py`, `guest_session.py` — host SSH launch and guest Pi environment, working directory and conversation lock.
- `sandbox/openrouter-vm/share.py` — persistent, fixed read-only WoW mount and guest module configuration.

## Tests asserting this spec

- `tests/test_openrouter_vm_deploy.py` — provisioning metadata, cloud-init, image verification, read-only export arguments and existing disk preservation.
- `tests/test_openrouter_vm_share.py` — preserved fstab content, escaped spaces, idempotence, conflicting entry rejection and mount/module configuration.
- `tests/test_openrouter_vm_proxies.py` — concrete HTTP transfer, denied requests and bidirectional relay behavior.
- `tests/test_openrouter_vm_session.py` — Pi launch environment, identity and configuration preservation, process-lifetime conversation locking, read-only export arguments and SSH arguments.
- `tests/test_openrouter_vm_runtime.py`, `tests/test_openrouter_vm_signatures.py` — runtime deployment, transient download retries, launcher-only preservation and real signature acceptance/rejection.

## Live evidence and gaps — 2026-10-30

- [x] Booted the KVM guest and installed tools and headless packages on its writable disk. The pinned image digest and signature, and both cloud-init schemas, were verified.
- [x] Transferred 13 log files before cutover; all transferred files matched their SHA-256 manifest. The final home/workspace archive matched its transferred SHA-256 and was extracted. The old session was stopped without cancellation and its record and data were retained.
- [x] Resumed the same conversation and reattached through the installed Rust client using SSH control and QUIC. The live Pi was running Ghidra analysis.
- [x] A direct UID-1000 check used the existing Xvfb and a disposable Wine prefix: llvmpipe worked and a Notepad X11 window remained present for two seconds after creation. No game was launched as a test.
- [x] Deployed the export fix without restarting Pi; the entire pre-migration saved history text remains an exact prefix of the resumed history after removing SGR color sequences. A real installed attachment resized the guest TTY and detached with exit 0 while preserving Pi and the existing user's geometry.
- [x] Activated the read-only WoW share after an approved clean VM restart. The agent read both client executables with matching host hashes; root write-open returned `EROFS` even after a writable client remount. The configured Syncthing path is Windows → `aso` → `agent-server`; no folder modes were changed. See [the data share record](../../sandbox/openrouter-vm/README.md#wow-installation-data-share).
- [x] Upgraded only the VM to Pi 1.0.0 and recovered the same conversation. Two normal installed attachments in a real TTY showed restored scrollback and live output; clean detach preserved the guest Pi. Both host canonical Pi installations remain at 0.80.3.
- [x] Delivered the mount notice directly in the attachment; the agent acknowledged it and read the mounted directory and `.build.info`. The 1,800-second check loop is active. See [the recovery record](../../sandbox/openrouter-vm/README.md#wow-installation-data-share).
- [ ] Observe the first scheduled check interval; registration is confirmed, but the interval has not yet elapsed.
- [ ] Verify restart lifetime without duplicate guest Pi, cancellation lifecycle, host reboot and guest updates. Sampled root network checks are not exhaustive isolation proof.

The exact evidence and limits are in the [migration record](../../sandbox/openrouter-vm/README.md#live-migration-evidence--2026-10-30).

## Out of scope

- WoW execution as an installation test, live game connections and changes to analysis code. The installation task does not prohibit the agent's separately authorized analysis.
- General internet access, arbitrary proxy targets, LAN access and host filesystem shares other than the specified read-only WoW data exception.
- Preservation of an in-progress model turn, unsent editor text or the old PTY process across conversation resume.

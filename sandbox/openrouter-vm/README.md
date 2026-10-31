# Writable OpenRouter VM

This is a KVM virtual machine, not a container. The guest has its own kernel and writable Arch Linux installation. The host continues to run Rust remote-agent; its Pi executable connects to the guest through private, key-only SSH.

## Provision

Install host dependencies with the registered Arch CLI:

```text
authsudo arch install qemu-system-x86 qemu-img xorriso
sandbox/openrouter-vm/deploy.sh --host agent-server
sandbox/openrouter-vm/deploy.sh --host agent-server --pi-image sha256:IMAGE_ID
sandbox/openrouter-vm/deploy.sh --host agent-server --launcher-only
```

`--pi-image` installs the exact `/opt/pi` bytes from an immutable host Docker image into the guest. It also installs the registered Arch CLI, the guest launcher, and the shared tool/graphics package list through the controlled mirror. It does not copy conversation data or credentials. Transient package-download failures have at most three attempts; permanent failures stop deployment. `--launcher-only` updates only the host helpers and guest launcher, without replacing the running Pi executable, installing packages or restarting services.

The provisioner verifies the pinned official cloud image's SHA-256 and signature against `arch-boxes.asc`, the release CI key published in the official arch-boxes README (primary fingerprint `1B9A16984A4E8CB448712D2AE0B78BF4326C6F8F`), creates a writable 64 GiB QCOW2 overlay, and starts the user service `wowdecomp-vm.service`. It assigns four virtual CPUs and 8 GiB RAM. State, SSH keys and serial output live in the owner-only `~/.local/share/openrouter-vm/` directory. Re-running deployment updates helper scripts without replacing the disk or restarting the running VM. Unknown or incomplete state fails explicitly.

The guest clock uses QEMU's host-backed RTC and KVM clock. External NTP is not allowed. Cloud-init masks the guest's network time synchronization and its blocking wait service during the early boot stage; otherwise the stock cloud image never reaches its final stage on the restricted network, so SSH startup would not run.

Normal guest work uses `agent` (UID 1000). Normal deployment must persist guest-only passwordless sudo with `agent ALL=(ALL:ALL) NOPASSWD: ALL`; live application of this policy is not yet verified. Connect through the host:

```text
ssh -t agent-server 'ssh -F ~/.local/share/openrouter-vm/ssh_config wowdecomp-vm'
```

After the sudo policy is deployed, install software as `agent` through the controlled Arch mirror:

```text
sudo -n /usr/local/bin/arch install <package>
```

Verified 2026-10-30: `deploy.sh --launcher-only` installed and validated the root-owned `0440` policy without restarting Pi. As `agent`, `sudo -n id` returned UID 0 and an actual `arch install strace` succeeded. The agent consumed the direct capability notice and independently installed ImageMagick with sudo. See `/tmp/wowdecomp-agent-admin-install-proof.log` and `/tmp/wowdecomp-admin-notice-screen.txt`. The WoW mount remains read-only; network restrictions are unchanged.

The guest does not have `authsudo`. For key-only root SSH administration, use `root@wowdecomp-vm` instead. Password authentication is disabled. The guest's SSH host key is generated before boot and pinned in the private known-hosts file.

For the standing restart authorization and recurring checks, see [operations](OPERATIONS.md).

## Network and packages

QEMU user networking uses `restrict=on` with IPv6 disabled. Its only guest forwards are `10.0.2.100:3128`, which relays to the existing fixed OpenRouter proxy, and `10.0.2.100:3129`, which permits repository GET/HEAD requests to `https://geo.mirror.pkgbuild.com` with upstream TLS verification. The package mirror entry uses the latter endpoint; Arch package signatures remain required. SSH is forwarded only on host loopback port 22230. The read-only WoW data exception below is the only host filesystem share. No host home, host key, SSH agent or management socket is shared with the guest.

The user approved this package-repository exception on 2026-10-30. It does not permit general internet or LAN access. Root inside the guest cannot alter the host's QEMU forwarding rules.

Capstone provides instruction decoding, Unicorn provides CPU emulation, Java runs Ghidra, and Ghidra provides headless analysis. Wine is installed but WoW is not launched as part of deployment or verification.

## WoW installation data share

On 2026-10-30, live Syncthing configuration showed folder `wow-install` (label `World of Warcraft Install`) as send-only on Windows desktop device `3NF6RRU`, at `C:\World of Warcraft`, shared only with `aso` (`4FP`). The `aso` copy is receive-only at `/syncthing/World of Warcraft` and shares with Windows and `agent-server`. The `agent-server` copy is receive-only at the same Linux path and lists Windows and `aso` as peers. This establishes the configured one-way flow Windows → `aso` → `agent-server`, not a direct Windows/server pair. Actual folder transfer was not observed; no Syncthing configuration was changed.

The exception exposes only `/syncthing/World of Warcraft` at the same path in the guest, read-only through 9p with share tag `wow-install`, `security_model=mapped-xattr`, and `readonly=on`. Its persistent guest mount uses `ro,nodev,nosuid,trans=virtio,version=9p2000.L,cache=none`; `noexec` is not required. The restriction applies to the shared data, not the guest's writable disk. Network restrictions remain unchanged.

Activated on 2026-10-30 through `deploy.sh` and a clean, approved VM power-off/start. Guest `findmnt` reports `wow-install`, type `9p`, `ro,nosuid,nodev`. The unprivileged agent read the full retail and Mists executables; both SHA-256 hashes matched the host. Guest root's write-open returned `EROFS`, including after a writable client remount; the host export stayed read-only and the client was returned to `ro`. Persistent fstab and module configuration survived boot. Details: `/tmp/wowdecomp-wow-share-access.log`.

Post-restart network checks still refused all eight direct public/LAN/host/metadata targets, gave no DNS response, disabled IPv6, accepted only the two fixed endpoints, and rejected proxy escape attempts. Details: `/tmp/wowdecomp-network-after-wow-share.log`.

The user approved upgrading only this VM to Pi 1.0.0 to recover from a malformed PE tool result containing C1 controls. Deployment with `--pi-image sha256:286842e2fa9a644a52a5194dfa6e42d98ad68c1a8bbb7f949fdf4370020ed273` succeeded; the guest runtime SHA-256 `076b0a6373e20035db208b1eed10dd39cc5d448198d2d1f26fc241f0b1c0bec6` matches the staged build. Both host canonical Pi installations remain at 0.80.3. See `/tmp/wowdecomp-pi1-deploy-fixed.log`.

The installed `remote-agent --host agent-server --user osso attach wowdecomp` command worked twice in real WezTerm TTY pane 173. Historical scrollback contained 1,987 nonblank lines, and live streaming and tool output appeared. Ctrl+backslash detached while preserving guest Pi PID 1529 and conversation UUID `e9f22590-027a-452a-bc36-a1312c9b5537`. See `/tmp/wowdecomp-pi1-reattach-history.txt`. Before resume, normalized-SGR exported text was identical and configuration and credentials were unchanged; see `/tmp/pi-tool-b906d1d385ad6458.log`.

The CLI control notice was queued but did not reach the active turn. A direct message in the normal attachment was consumed: the agent acknowledged the full installation and executed a read of the mounted directory and `.build.info`, then began checking build identity, adjacent DLLs and Data. See `/tmp/wowdecomp-direct-notice-screen.txt`. The 1,800-second Pi loop is active; its first scheduled interval has not elapsed. Post-upgrade network probes passed: eight direct targets blocked, two DNS probes unanswered, IPv6 disabled, both allowed endpoints HTTP 200, and six proxy escape requests rejected. See `/tmp/wowdecomp-network-after-pi1.log`.

A deployment rerun initially failed when `share.py` attempted to chmod the read-only mount. Commit `b11138a` sets permissions only when creating the directory and preserves existing modes. Three focused tests passed, and the actual redeployment succeeded.

## Migration order

1. Provision and test the VM while the existing container session remains available.
2. Transfer logs and record integrity evidence before session cutover.
3. Stop only the old managed session without cancellation, archive its managed record, and transfer its final Pi home and workspace.
4. Select the VM Pi wrapper in the intended account's project map and resume the same conversation UUID.
5. Test normal installed attachment, terminal resize and clean detach; retain the original data until acceptance.

The guest Pi launcher keeps `/home/agent`, `/workspace`, the existing model settings and the original Pi executable. It reads the dedicated OpenRouter key from `/etc/openrouter-vm/openrouter.key` without placing it on the command line. It does not reset saved settings at each launch. A process-lifetime lock refuses concurrent guest Pi launches for the same conversation. Terminal-history export remains a read-only Pi operation, not a migration backup.

A resumed conversation is not a preserved process. An in-progress model turn, old PTY and unsent editor text are not restored. Existing background jobs must be reconciled before cutover rather than assumed to transfer.

## Live migration evidence — 2026-10-30

On `agent-server`, the VM booted with `/dev/kvm`, guest kernel 7.2.9 and a 64 GB root filesystem. The host user service is `wowdecomp-vm.service`. The pinned official cloud image was `20261015.611680`, SHA-256 `08754e28d91b1680086c6211ef4df13559e766fff7cdb4006489b14b5d281f08`; its CI signature verified under primary key `1B9A16984A4E8CB448712D2AE0B78BF4326C6F8F`, signing key `656E4C5AC1CC3B86E539D97E343635A6859A9174`. Both cloud-init schemas validated.

The guest installed the shared tools and headless graphics packages through the registered root `arch` command using `--pi-image` with the original immutable image `sha256:54cc46c214c749bbb8eb5ae43996608b8342798edf63f7acc61bce7e32193b98`. The source install log is `/tmp/wowdecomp-vm-runtime-install-headless.log`; it records the completed 101-package transaction. This installation used root SSH and does not verify the new agent sudo policy.

With an added guest default route, root could fetch Arch `core.db` and the OpenRouter model catalog (HTTP 200; 456 entries). Sampled connections to `1.1.1.1:443`, `192.168.2.1:80`, host `192.168.2.149:22`, gateway `10.0.2.2:22` and `:80`, and metadata `169.254.169.254:80` were refused. UDP DNS to `10.0.2.3` and `1.1.1.1` timed out. These checks do not prove exhaustive isolation. Guest NTP services were masked at early boot; host time was not changed.

A post-migration root check repeated eight direct TCP attempts with a guest default route and confirmed refusal, including direct OpenRouter HTTPS. Both allowed endpoints returned HTTP 200. Foreign-host, absolute-URL and CONNECT requests were rejected on both forwards (400/501). The live QEMU command retained `restrict=on,ipv6=off`; SSH listened only on `127.0.0.1:22230` and refused the host's LAN address. Host-side control connections to public HTTPS and host SSH succeeded. Details are in `/tmp/wowdecomp-network-isolation-current.log` on the controller and `/tmp/wowdecomp-network-isolation-current.json` on `agent-server`.

Thirteen log files were transferred before cutover; every transferred file matched its SHA-256 manifest. Copies are in guest `/var/log/wowdecomp-migration-20261030` and host `~/.local/share/openrouter-vm/migration-20261030`. After the agent checkpointed and became idle, the approved Wine and Xvfb processes were stopped. Only `remote-agent-rs@wowdecomp.service` was stopped, without cancellation; its record was archived at `~/.local/state/remote-agent-rs/archives/wowdecomp-container-20261030`. The final home/workspace archive was 1,313,249,280 bytes, SHA-256 `f78fa62d5a48a4df90a6cc6dd7990e73396cdd9303896037780a960b1657020e`; the transferred hash matched and the archive was extracted. Original OCI volumes, the stopped container, and the stopped pre-tools and pre-headless temporary-volume keepers remain available.

The guest retained the home and workspace paths. Its models file changed only the OpenRouter base URL from `172.30.253.1` to `10.0.2.100`. The dedicated key was copied privately to `/etc/openrouter-vm/openrouter.key`, owned by UID 1000 with mode `0400`. Local and remote `osso` project maps select `~/.local/share/openrouter-vm/session.py` for `openrouter-sandbox`; the old cancel hook was removed. Installed Rust `start --resume` resumed conversation `e9f22590-027a-452a-bc36-a1312c9b5537`. Host server PID 3692404, SSH child 3692408 and guest Pi PID 1190 were observed. The normal user reattached in pane 173 with `remote-agent --host agent-server --user osso attach wowdecomp`; the old client had first timed out on retry. Pi was running Ghidra analysis in the VM, with the DeepSeek model and medium effort unchanged. Transport is SSH control plus QUIC, not legacy Mosh.

In the original container, the final headless-tools image was `sha256:074f14fd88786739c7fc1371ae607bee4addc1e13c209a81a7e307df52856547`. The agent reported that Xvfb worked and a Wine client executed `.text`; this is not independent proof of decryption. See [container tool proof](../openrouter-runtime/README.md#tools-only-deployment-proof--2026-10-30).

Export fix `f8790be` was deployed through `--launcher-only` without restarting Pi. Removing SGR color sequences from the read-only 120-column exports showed that all 1,240,267 bytes of pre-migration saved history remained an exact prefix of the 1,302,796-byte post-migration history. Raw ANSI hashes differ because styles differ and new work was appended; no raw-byte equality is claimed.

A separate real PTY ran the exact installed `remote-agent --host agent-server --user osso attach wowdecomp` command. A resize to 120 columns by 30 rows reached the guest Pi TTY; Ctrl+backslash detached with exit 0 and restored the remaining user's 269-column by 50-row geometry. Host server and guest Pi identities remained unchanged. The user's terminal was reattached, and the agent received the completed-cutover message while continuing its task. Operational details are in `/tmp/wowdecomp-vm-attach-operational.json`.

A direct, non-game graphics check in the VM used the existing agent-owned Xvfb `:99` and a disposable Wine prefix as UID 1000. `glxinfo` reported llvmpipe (LLVM 23.1.4); Wine created `Untitled - Notepad` in 12.36 seconds and the X11 window remained present for two more seconds. Only the disposable prefix's Wine processes were stopped; the existing Xvfb and session were preserved. Details are in `/tmp/wowdecomp-headless-window-direct-result.log` on the controller.

**Not yet proven:** host reboot, guest package/kernel updates, exhaustive isolation, cancellation lifecycle, and restart without a duplicate guest Pi. These are not installation or migration gates. See the [contract](../../docs/specs/openrouter-vm.md).

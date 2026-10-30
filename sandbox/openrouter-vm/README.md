# Writable OpenRouter VM

This is a KVM virtual machine, not a container. The guest has its own kernel and writable Arch Linux installation. The host continues to run Rust remote-agent; its Pi executable connects to the guest through private, key-only SSH.

## Provision

Install host dependencies with the registered Arch CLI:

```text
authsudo arch install qemu-system-x86 qemu-img xorriso
sandbox/openrouter-vm/deploy.sh --host agent-server
```

The provisioner verifies the pinned official cloud image's SHA-256 and signature against `arch-boxes.asc`, the release CI key published in the official arch-boxes README (primary fingerprint `1B9A16984A4E8CB448712D2AE0B78BF4326C6F8F`), creates a writable 64 GiB QCOW2 overlay, and starts the user service `wowdecomp-vm.service`. It assigns four virtual CPUs and 8 GiB RAM. State, SSH keys and serial output live in the owner-only `~/.local/share/openrouter-vm/` directory. Re-running deployment updates helper scripts without replacing the disk or restarting the running VM. Unknown or incomplete state fails explicitly.

Normal guest work uses `agent` (UID 1000). Root administration uses a separate SSH login with the dedicated key; password authentication is disabled. The guest's SSH host key is generated before boot and pinned in the private known-hosts file.

## Network and packages

QEMU user networking uses `restrict=on` with IPv6 disabled. Its only guest forwards are `10.0.2.100:3128`, which relays to the existing fixed OpenRouter proxy, and `10.0.2.100:3129`, which permits repository GET/HEAD requests to `https://geo.mirror.pkgbuild.com` with upstream TLS verification. The package mirror entry uses the latter endpoint; Arch package signatures remain required. SSH is forwarded only on host loopback port 22230. No host filesystem, SSH agent or management socket is shared with the guest.

The user approved this package-repository exception on 2026-10-30. It does not permit general internet or LAN access. Root inside the guest cannot alter the host's QEMU forwarding rules.

Capstone provides instruction decoding, Unicorn provides CPU emulation, Java runs Ghidra, and Ghidra provides headless analysis. Wine is installed but WoW is not launched as part of deployment or verification.

## Migration order

1. Provision and test the VM while the existing container session remains available.
2. Transfer logs and record integrity evidence before session cutover.
3. Stop only the old managed session without cancellation, archive its managed record, and transfer its final Pi home and workspace.
4. Select the VM Pi wrapper in the intended account's project map and resume the same conversation UUID.
5. Test normal installed attachment, terminal resize and clean detach; retain the original data until acceptance.

The guest Pi launcher keeps `/home/agent`, `/workspace`, the existing model settings and the original Pi executable. It reads the dedicated OpenRouter key from `/etc/openrouter-vm/openrouter.key` without placing it on the command line. It does not reset saved settings at each launch. A process-lifetime lock refuses concurrent guest Pi launches for the same conversation. Terminal-history export remains a read-only Pi operation, not a migration backup.

A resumed conversation is not a preserved process. An in-progress model turn, old PTY and unsent editor text are not restored. Existing background jobs must be reconciled before cutover rather than assumed to transfer.

## Verification status

Provisioning, guest package installation, log transfer and session migration require live verification. Fixture tests alone do not establish guest isolation or transport behavior. See the [contract](../../docs/specs/openrouter-vm.md) and [existing container tool proof](../openrouter-runtime/README.md#tools-only-deployment-verification--2026-10-30).

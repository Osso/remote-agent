# Clank Pi sandbox

Dedicated agent-server containers run Pi with OpenRouter model `deepseek/deepseek-v4.1-flash-1017` and the reviewed clank client. Each remote-agent conversation gets its own container; only dedicated Pi-home and workspace volumes are shared. No host home, SSH agent, Docker socket or existing provider credentials enter the image. The installed custom Pi binary and support assets are copied without user configuration.

`td` is built from clank revision `b4f8f6d6308ef5217637f34a24cde016b8b38afc` in an unprivileged, network-filtered builder. The reviewed builder pins Node22.21.0, esbuild0.24.0 and postject1.0.0-alpha.6. npm tools remain an upstream supply-chain dependency; isolation does not authenticate their authors. Build and runtime image IDs and executable SHA-256 hashes are recorded by deployment.

## Trust boundaries

- Runtime UID1000, all capabilities dropped, no-new-privileges, read-only root, bounded memory/CPU/processes, IPv6 disabled, no published ports. Installed `td` cannot be replaced; `TD_NO_UPDATE=1` independently disables its unsafe same-server-checksum updater.
- Host nftables rules apply to the dedicated `br-clank` interface before Docker forwarding: deny host input, nonpublic IPv4/connected routes, all IPv6, and unsolicited inbound traffic. Public IPv4 remains subject to Docker's normal rules. Public DNS servers are explicit; no LAN DNS proxy is configured. Same-bridge communication is disabled.
- The firewall service must be active before the session wrapper starts a container. It is enabled for boot and ordered before Docker. Containers do not automatically restart; remote-agent recovery goes through the checked wrapper.
- Dedicated key is a read-only file mount, not an image layer or Docker environment/config value. Pi's launcher reads it without printing it and constructs an OpenRouter-only environment. Pi implementations for other providers remain built in, but no credentials for them are provisioned. Automatic extensions, skills, context files, prompt templates and themes are disabled.
- Containers share the host kernel. Code in a container can read/exfiltrate its own OpenRouter key and execute other downloaded programs from writable volumes. Use a dedicated spending-capped key. Public internet access is not a domain allowlist. Root/Docker administrators can bypass these boundaries.

## Deploy

From the remote-agent repository:

```text
sandbox/deploy.sh --host agent-server --key-file /private/path/openrouter.key
```

Deployment preserves existing project maps and workloads, installs only the `clank` project entry, and does not log in to clank, configure MCP, install Ghidra/compiler toolchains, or install host `td`. Key contents never belong in tracked files. The ordinary remote-agent worker deployment remains `./deploy.sh`; use both when its protocol changes.

## Use

```text
remote-agent start clank clank --agent-harness pi
remote-agent attach clank
remote-agent status clank
remote-agent cancel clank
```

Detach with Ctrl+backslash; do not cancel to detach. History export uses a transient offline container with the same private Pi-home/workspace volumes. Resume uses the recorded conversation. Cancellation removes only the selected conversation container, including its descendants, after stopping its host launcher; saved Pi history/workspace remains.

`td --help` is available inside Pi's shell. Reviewed source is `/opt/clank-together`; writable work is `/workspace`. No clank project is authenticated or automatically set up. Read-only installation is not a promise that subsequently cloned workspaces or server-provided tools are safe.

## Verification

Unit proofs: `tests/test_sandbox_launch.py`, `tests/test_sandbox_firewall.py`, `tests/test_sandbox_session.py`; lifecycle routing also uses `tests/test_remote_agent.py` and `tests/test_history.py`. Live acceptance must check public connectivity, blocked host/LAN/private/metadata/IPv6, immutable td, provider configuration, selected-container cancellation, and actual SSH-history/Mosh-TTY attach/detach. Network rules alone and a help command are not end-to-end proof.

Deployment and live acceptance are pending; no completion claim yet.

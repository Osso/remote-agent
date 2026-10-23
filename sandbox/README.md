# Clank Pi sandbox

The configured agent-server sandbox runs Pi with OpenRouter model `deepseek/deepseek-v4.1-flash-1017` and the reviewed clank client. Each remote-agent conversation gets its own container; only dedicated Pi-home and workspace volumes are shared. No host home, SSH agent, Docker socket or existing provider credentials enter the image. The installed custom Pi binary and support assets are copied without user configuration.

`td` is built from clank revision `804ebd15c25d9371eb45e836bcf1d22a69241440` in an unprivileged, network-filtered builder. The reviewed builder pins Node22.21.0, esbuild0.24.0 and postject1.0.0-alpha.6. npm tools remain an upstream supply-chain dependency; isolation does not authenticate their authors. Build and runtime image IDs and executable SHA-256 hashes are recorded by deployment.

The source review found no observed malware; that is not a safety guarantee. The updater was judged unsafe independently of that finding. Observed deployment/live coverage is recorded below; independent final verification remains pending.

## Trust boundaries

Configured controls follow; the verification section distinguishes observed enforcement from unproven requirements.

- Runtime UID1000, all capabilities dropped, no-new-privileges, read-only root, 4GB RAM limit, bounded CPU/processes, IPv6 disabled, no published ports. Installed `td` cannot be replaced; `TD_NO_UPDATE=1` independently disables its unsafe same-server-checksum updater.
- `/tmp` uses a per-container anonymous disk-backed Docker volume instead of the 512MiB tmpfs, with no host bind mount. It is discarded on container cancellation/replacement; private Pi-home/workspace volumes remain unchanged. Temporary storage now shares host disk capacity, without a separate volume quota. Unlike the old tmpfs, the volume does not request `nosuid,nodev`; `no-new-privileges` and dropped capabilities remain enabled.
- Host nftables rules apply to the dedicated `br-clank` interface before Docker forwarding: deny host input, nonpublic IPv4/connected routes, all IPv6, and unsolicited inbound traffic. Public IPv4 remains subject to Docker's normal rules. Public DNS servers are explicit; no LAN DNS proxy is configured. Same-bridge communication is disabled.
- The firewall service must be active before the session wrapper starts a container. The installed service is enabled and ordered before Docker; a full host reboot has not been tested. Containers do not automatically restart; remote-agent recovery goes through the checked wrapper.
- Dedicated key is a read-only file mount, not an image layer or Docker environment/config value. Pi's launcher reads it without printing it and constructs an OpenRouter-only environment. Pi implementations for other providers remain built in, but no credentials for them are provisioned. Skills, context files, prompt templates and themes are disabled. Built-in first-party extensions load (sub-agents, mailbox, goal, loop); `settings.json` `disabledExtensions` drops host-only ones (Claude memory, Codex, bwrap, session archive, self-restart, debug REPL). Sub-agents run in-process on the same OpenRouter model: `settings.json` `agents` overrides Pi's built-in `implement`/`explore`/`verifier`/`reviewer`/`documentation-update` profiles, which otherwise pin unreachable `openai/*` models. The resident Supervisor (goal review, `ask_supervisor`) uses `supervisorModel` on the same DeepSeek model with `PI_KB_DIR=/home/agent/kb`; it needs Pi `45e67c2b1` or later, so full deploys pass `--pi-install` pointing at a build of that revision (current deployed Pi build: `dbc28f233`, supplied from `~/.local/share/pi` after the approved laptop/agent-server deployment). Bundled `/periodic-checks` and `/exit` require no user-extension files; [periodic checks](../README.md#optional-periodic-checks) remain inactive without a project checklist.
- Containers share the host kernel; container/runtime vulnerabilities remain a residual risk. Code in a container unavoidably can read/exfiltrate its own OpenRouter key and execute other downloaded programs from writable volumes. Use a dedicated spending-capped key. Public internet access is not a domain allowlist. Root/Docker administrators can bypass these boundaries.

## Deploy

From the remote-agent repository:

```text
sandbox/deploy.sh --host agent-server --key-file /private/path/openrouter.key
```

Omit `--key-file` on redeploy to keep the provisioned key.

Deployment preserves existing project maps and workloads, installs only the `clank` project entry, and does not log in to clank, configure MCP, install Ghidra/compiler toolchains, or install host `td`. The image includes Arch `wine` so the server-provided MSVC `cl.exe` can run for `td check`. Key contents never belong in tracked files. The ordinary remote-agent worker deployment remains `./deploy.sh`; use both when its protocol changes. For a host-wrapper-only change, `sandbox/deploy.sh --wrappers-only` updates routing without rebuilding td/Pi or touching credentials. `sandbox/deploy.sh --runtime-only` rebuilds only the Pi launcher's image layer; it preserves the audited td/Pi bytes and credentials.

## Use

```text
remote-agent start clank clank --agent-harness pi
remote-agent attach clank
remote-agent status clank
remote-agent cancel clank
```

Detach with Ctrl+backslash; do not cancel to detach. History export uses a transient offline container with the same private Pi-home/workspace volumes. Resume uses the recorded conversation. Cancellation removes only the selected conversation container, including its descendants, after stopping its host launcher; its anonymous `/tmp` volume is discarded, while saved Pi history/workspace remains. The host session wrapper proxies SIGWINCH to the running container so Pi repaints after attachment.

The mapped host directory is only the worker's launch directory, not a workspace mount. `remote-agent sync` and `fetch` still address that host directory; they do not transfer the container's named-volume workspace.

`td --help` is available inside Pi's shell. Reviewed source is `/opt/clank-together`; writable work is `/workspace`. Deployment does not set up a clank project. Workspace setup is a one-time manual step: run `td setup https://saty.dev /workspace/wowdecomp --token <token>` and `td toolchain install` inside the running container (`docker exec -i`, token on stdin); the token then lives in the workspace volume's `.td` config, readable by the agent. Pi has no MCP; the agent uses the `td` CLI. Image settings set `approvalPolicy: auto-approve` so the agent runs unattended inside the container boundary; an existing home volume keeps its own `settings.json` (set there by hand on 2026-10-23). Read-only installation is not a promise that subsequently cloned workspaces or server-provided tools are safe.

## Verification

### Temporary storage — verified 2026-10-23

Wrapper `0d7a2b0` deployed through `sandbox/deploy.sh --wrappers-only`. Inside the resumed conversation, `/tmp` is ext4, mode 1777, writable as UID1000; a 600MiB allocated write and readback passed and the test file was removed. A separate `--help` conversation proved cancellation deletes its anonymous temporary volume while preserving named home/workspace volumes. Root remains read-only, memory remains 4GiB, all capabilities remain dropped, and `no-new-privileges` remains set. Normal SSH/Mosh attachment and Ctrl+backslash detach passed; conversation `50a99d03` continued running. Independent gate: 23 wrapper tests, Ruff check and format check passed.

### Observed coverage — verified 2026-10-22

- Network: `/tmp/clank-network-proof.json` records HTTP200 from public OpenRouter and clank, authenticated OpenRouter HTTP200 with a credit limit, UID1000, timeouts for tested host/LAN/bridge/private/CGNAT/metadata addresses, and IPv6 disabled (`1`). `/tmp/clank-restart-network-proof.json` confirms public access and tested host/metadata restrictions persisted after proof conversation B restarted. These are sampled endpoints, not exhaustive address-space proof or a host-reboot test.
- Model: `/tmp/clank-model-proof.json` records one requested smoke, response sentinel, clean detach and exit0; main observed approximately $0.002 in the UI.
- Resume/TTY: `/tmp/clank-history-attach-proof.json` records normal SSH-history preload followed by actual Mosh attachment to the same conversation, restored response, one approved harmless `end_turn`, detach and exit0. `/tmp/clank-repaint-proof.json` separately records same-size 48×200 history before live-screen clear, live model footer/response/workspace, detach and exit0.
- Scoped cancellation: main observed all four exact host identities for proof A (init, Pi, exec parent, sleep) gone while B and the original conversation remained running. B then restarted with network restrictions intact and was canceled. Backing identity evidence is remote `~/.local/share/clank-sandbox/cancel-proof.json`; it was not reread during this docs-only update.

### Installed inventory — 2026-10-23

Deployed from setup `c425b21`, clank `804ebd1`, Pi `dbc28f233`; conversation `50a99d03` resumed with its existing workspace, credentials and disk-backed `/tmp`. Inventory reports bundled `periodic-checks` and `exit-command`. A disposable TUI returned disabled scheduler status and exited through `/exit`; no periodic checklist was installed. Normal attachment and detach were checked without exiting the active conversation.

| Artifact | Image ID / SHA-256 |
| --- | --- |
| Runtime image | `8abcf7c44d95b05780baab9eff7562ccfe72839b17fad5ab08f6382fbaae5afd` (setup `c425b21`) |
| td | `87690f978c1903b25bed412f7be609fdeaeebcf28c10d4e7f8f66fb14667980d` |
| Pi | `acea31899cfcf247f629414e6a42ae8742766e71060f789f6a2dc331969e1268` (Pi `dbc28f233`) |

Verified in the container: `ask_supervisor` answered on `openrouter/deepseek/deepseek-v4.1-flash-1017`; `spawn_agent` `implement` and `verifier` children run on the same model; a `--prompt-file` sent while the conversation was mid-run reached its transcript at restart.

### Independently reconciled installed inventory — 2026-10-22 (superseded)

Original conversation `50a99d03-d10f-4e31-a252-e7fc85bd349d` remains running, detached, in Docker container `clank-pi-78d0ab79a48e37d22876fab3180cf226`. Runtime launcher and wrapper source are `dde9bf5`; worker source remains identical to deployed revision `1e47784`. Final behavioral gate covers source `ddc17f7`.

| Artifact | Image ID / SHA-256 |
| --- | --- |
| Runtime image | `8cc443908d846aba1dbdc832f22d2808af943c6fcdd9cf8ad9e2d0a02fb33846` |
| td | `5f81ab83a16cb64058d1cc1f0b0130933744925eecdb164a0815165cdbe6a0c9` |
| Pi | `70e7eaa4562656febee169ad361d3134c1c7d9b41dacd1f8a7da07c7aca28d35` |

### Independent gate — verified 2026-10-22

Bounded PASS at `ddc17f7`: 62 distinct targeted tests across current/retained unchanged scopes, new sandbox Python lint and formatting clean. This is not a whole-project pass: 25 unrelated pre-existing Ruff diagnostics remain. Exact commands, revisions, artifacts and limits are recorded in `/tmp/clank-final-verification.md`; the current source and installed wrapper/launcher hashes were reconciled independently.

Final normal resume and same-size 48×200 reattachment both exited0 using the original conversation; `/tmp/clank-final-resume-proof.bin` and `/tmp/clank-repaint-proof.bin` preserve separate evidence. The latter contains SSH-restored response before live-screen clear and actual live model/footer/response/workspace afterward. No model request was repeated for final source-format/fixture fixes.

Actual full-host-reboot behavior remains untested; enabled firewall service and ordering were inspected without rebooting. Predeployment map-byte snapshots were not captured independently. See the [spec](../docs/specs/clank-sandbox.md) for scope and remaining proof gaps.

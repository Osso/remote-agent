# Independent OpenRouter sandbox

The independent OpenRouter Pi runtime lives in `sandbox/openrouter-runtime/`. It is separate from the [Clank sandbox](clank-sandbox.md). Its [runtime documentation](../../sandbox/openrouter-runtime/README.md) describes operation and isolation.

## What it must do

- [ ] Provide a separate `openrouter-sandbox` remote-agent project, image, network, state, credentials and persistent Pi-home and workspace volumes without changing the Clank sandbox.
- [ ] Run Pi as an unprivileged user with a read-only root filesystem, dropped capabilities and no-new-privileges. Keep the mapped host project out of the container; its workspace is a separate Docker volume, not the directory used by remote-agent sync or fetch.
- [ ] Use Particle-1 as the default for Pi, the supervisor and configured agent profiles, and allow selection of Particle-1, MiniMax M3.1 Flash and DeepSeek V4.1 Flash 1017 through OpenRouter. Install packaged settings and model configuration at each launch.
- [ ] Route model requests through the host-side reverse proxy to the fixed `openrouter.ai` upstream over verified TLS. Do not provide a general forward proxy or CONNECT tunnel.
- [ ] Use the dedicated internal `br-or-sandbox` bridge with IPv6 and inter-container communication disabled. Deny bridge forwarding in both directions and host input except IPv4 TCP to the bridge proxy address and port; do not provide external DNS to the container.
- [ ] Require the isolation and proxy services before sessions start, including after host reboot.
- [ ] Provision a dedicated OpenRouter key outside the image and command line, mount it read-only, and retain it across deployments when no replacement is provided. Use a spending-limited key.
- [ ] Preserve remote-agent session start, resume, history, attachment and selected-conversation cancellation without stopping other conversations.
- [ ] Support configuration-only image updates without restarting services or existing containers or changing credentials; optionally update only live home models, leaving live settings unchanged.

## How it works

- [Runtime operation and isolation](../../sandbox/openrouter-runtime/README.md).
- [Pi configuration](../../sandbox/openrouter/README.md).

## Implementation inventory

- `sandbox/openrouter-runtime/deploy.sh`, `deploy.py` — deployment entry point, staging and project mapping.
- `sandbox/openrouter-runtime/host_deploy.py` — host firewall, proxy service, network, image and volume setup.
- `sandbox/openrouter-runtime/Dockerfile`, `launch.py` — container image, credentials and Pi configuration.
- `sandbox/openrouter-runtime/session.py` — conversation container lifecycle and cancellation.
- `sandbox/openrouter_proxy.py` — fixed-upstream host reverse proxy.
- `sandbox/openrouter/models.json`, `settings.json` — provider, models and Pi profiles.

## Tests asserting this spec

- `tests/test_openrouter_firewall.py` — firewall policy and container network options.
- `tests/test_openrouter_proxy.py` — reverse-proxy request and upstream behavior.
- `tests/test_openrouter_runtime.py` — runtime configuration, staging and session behavior.

On 2026-10-30, the three targeted test files passed (10 tests and 10 subtests). Live proof is recorded in the [runtime documentation](../../sandbox/openrouter-runtime/README.md#deployment-verification). Contract items remain unchecked where full lifecycle coverage is missing.

## Known gaps (current cycle)

- [ ] Verify complete session lifecycle, including history restoration and scoped cancellation, on the intended host.
- [ ] Verify service ordering and isolation after an actual host reboot.

## Out of scope

- Preventing sandbox code from reading its own key or sending it to OpenRouter. Use a dedicated spending-limited key; the fixed upstream is not a guarantee against disclosure through that service.
- Eliminating container escape, shared-kernel, image or dependency risks. Host root and Docker administrators are outside the isolation boundary.
- Treating the mapped host project as the container workspace or claiming that remote-agent sync/fetch transfers the volume contents.

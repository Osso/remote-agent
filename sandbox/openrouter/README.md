# OpenRouter sandbox Pi configuration

These files are separate from the Clank configuration. Deployment must install them as the sandbox Pi agent's `models.json` and global `settings.json`. Replace `http://openrouter-proxy.invalid:3128` in `httpProxy` with the dedicated proxy address reachable from the container. The `.invalid` address is an intentionally nonfunctional placeholder. Pi applies this setting to HTTP and HTTPS proxy environment variables; it is not a network isolation policy.

The provider uses `OPENROUTER_API_KEY`; supply it through the sandbox's deployment mechanism, not these files. Particle-1 is the default for Pi, the supervisor, and agent profiles; both models are enabled for selection. Metadata reflects the OpenRouter models API on 2026-10-30. Costs are per million tokens and may change. Pi supports text and image input; OpenRouter also lists video input for MiniMax.

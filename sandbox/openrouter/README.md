# OpenRouter sandbox Pi configuration

These files are separate from the Clank configuration. The [independent runtime](../openrouter-runtime/README.md) stages them as the sandbox Pi agent's `models.json` and global `settings.json`: it removes the placeholder `httpProxy` and sets the OpenRouter base URL to the host-side fixed-upstream reverse proxy. See the [sandbox contract](../../docs/specs/openrouter-sandbox.md) for isolation and verification limits.

The provider uses `OPENROUTER_API_KEY`; supply it through the sandbox's deployment mechanism, not these files. Particle-1 is the default for Pi, the supervisor, and agent profiles; both models are enabled for selection. Metadata reflects the OpenRouter models API on 2026-10-30. Costs are per million tokens and may change. Pi supports text and image input; OpenRouter also lists video input for MiniMax.

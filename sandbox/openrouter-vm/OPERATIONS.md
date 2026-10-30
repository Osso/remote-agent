# wowdecomp operating approval

## Standing authorization — 2026-10-30

The user authorized: “you can restart wowdecomp at anytime it is safe”. Do not request approval again for a covered safe restart.

This applies to `wowdecomp`, account `osso` on `agent-server`, project `openrouter-sandbox`, conversation `e9f22590-027a-452a-bc36-a1312c9b5537`. Reconcile that identity before acting. Checkpoint active work, preserve files and history, and do not interrupt an unsafe write or unrelated process. If safety cannot be established, leave it running and report the blocker. This approval does not authorize destructive recovery or expanded access.

## Recurring check

After the WoW data mount is active, check every 30 minutes using the normal installed command:

```text
remote-agent --host agent-server --user osso attach wowdecomp
```

Observe before input. Resolve routine prompts and recoverable failures only within the existing authorized task. Safe target restarts are covered above. Do not invent another task, raise spending limits, expose credentials, or approve new network destinations. Do not restart a session intentionally stopped by the user or whose task is complete.

Keep the [network policy](README.md#network-and-packages) and [read-only WoW share](README.md#planned-wow-installation-data-share) unchanged. Detach check clients with Ctrl+backslash without stopping Pi. Escalate only a user-owned decision or a real blocker.

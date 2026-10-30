# Independent OpenRouter sandbox

This runtime is separate from Clank. It uses the `openrouter-sandbox` project and network, `br-or-sandbox` bridge, its own image, volumes, service units, state directory and key. Its fixed subnet is `172.30.253.0/24`; deployment fails if Docker cannot reserve it. It does not mount the mapped host project into the container. The project mapping provides the remote-agent working directory; the sandbox workspace is a separate Docker volume.

Deploy only when authorized:

```text
sandbox/openrouter-runtime/deploy.sh --host agent-server --pi-install /home/osso/.local/share/pi --local-project /absolute/local/project --key-file /private/openrouter.key
```

To update only the packaged Pi settings and models for future starts, without restarting services or existing containers or changing credentials:

```text
sandbox/openrouter-runtime/deploy.sh --host agent-server --config-only
```

This builds a new image from the image in `runtime.json` and changes only its image reference. Add `--update-live-models` to also atomically replace `models.json` in the shared Pi home using a short-lived, networkless container. It does not change the live `settings.json` or the running Pi process; refresh models in Pi to load the file. Future starts still install both packaged files into the shared home, so starting another session can change files used by an existing session.

The key file must be owner-only. Its contents travel over SSH stdin and are stored in the target user's private state directory. Omit `--key-file` on later deployments to retain the existing key. Use a separate spending-limited key. Container processes can read that key; the proxy does not prevent an agent from sending it to OpenRouter.

The firewall drops sandbox forwarding in both directions and all bridge input except IPv4 TCP to the host bridge's proxy port. The Docker network is internal and IPv6 is disabled. The container DNS server is set to loopback, rather than a public or host resolver. Pi's OpenRouter base URL targets an HTTP reverse proxy on the bridge, which forwards only to OpenRouter over verified TLS. The proxy service requires the firewall and Docker, and the session wrapper checks the proxy service before starting. Docker administrators and host root are outside this boundary.

At each launch, the image replaces `settings.json` and `models.json` in the home volume with its packaged configuration. Other home data persists. Do not run different image versions concurrently against that shared home.

## Deployment verification

Verified 2026-10-30 on `agent-server`: session `wowdecomp` runs in the independent sandbox. `/workspace/wowdecomp15890` matches the transfer archive, and `/workspace/Wow.exe` matches the source SHA-256. Particle-1 and MiniMax M3.1 Flash both completed a Pi `read` tool call. The proxy returned OpenRouter's model catalog and rejected alternative hosts and CONNECT; sampled direct public, LAN, host, IPv6 and DNS connections failed. Both services were enabled and active. These are sampled network checks, not exhaustive proof or a host-reboot test.

Attach with `remote-agent attach wowdecomp --host agent-server`; detach with Ctrl+backslash. Attachment and detach were tested in a terminal. The default model is Particle-1; select MiniMax through Pi's model selector. The copied workspace is separate from the host project used by remote-agent sync/fetch.

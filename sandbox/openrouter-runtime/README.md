# Independent OpenRouter sandbox

This runtime is separate from Clank. It uses the `openrouter-sandbox` project and network, `br-or-sandbox` bridge, its own image, volumes, service units, state directory and key. Its fixed subnet is `172.30.253.0/24`; deployment fails if Docker cannot reserve it. It does not mount the mapped host project into the container. The project mapping provides the remote-agent working directory; the sandbox workspace is a separate Docker volume.

Deploy only when authorized:

```text
sandbox/openrouter-runtime/deploy.sh --host agent-server --pi-install /home/osso/.local/share/pi --local-project /absolute/local/project --key-file /private/openrouter.key
```

The key file must be owner-only. Its contents travel over SSH stdin and are stored in the target user's private state directory. Omit `--key-file` on later deployments to retain the existing key. Use a separate spending-limited key. Container processes can read that key; the proxy does not prevent an agent from sending it to OpenRouter.

The firewall drops sandbox forwarding in both directions and all bridge input except IPv4 TCP to the host bridge's proxy port. The Docker network is internal and IPv6 is disabled. The container DNS server is set to loopback, rather than a public or host resolver. Pi's OpenRouter base URL targets an HTTP reverse proxy on the bridge. The proxy must send requests only to the fixed OpenRouter upstream over verified TLS; its implementation and integration require verification before deployment. The proxy service requires the firewall and Docker, and the session wrapper checks the proxy service before starting. Docker administrators and host root are outside this boundary.

The home volume retains Pi configuration across image rebuilds. Changes to the image's initial settings do not overwrite an existing home volume. No deployment or live network enforcement test is implied by the presence of these files.

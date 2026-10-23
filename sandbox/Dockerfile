# The deployment records the resolved base-image ID and installed Pi/td hashes.
FROM archlinux:base
# wine runs the server-provided MSVC cl.exe for td's local checks.
RUN pacman -Syu --noconfirm --needed ca-certificates git python ripgrep icu curl wine \
    && pacman -Scc --noconfirm
RUN useradd --uid 1000 --create-home --shell /bin/bash agent \
    && install -d -o agent -g agent -m 0700 /home/agent/.config/pi/agent /home/agent/.remote-agent-prompts \
    && install -d -o agent -g agent /workspace /opt/sandbox
COPY pi/ /opt/pi/
COPY td /usr/local/bin/td
COPY clank/ /opt/clank-together/
COPY launch.py /opt/sandbox/launch.py
COPY settings.json models.json /home/agent/.config/pi/agent/
RUN chmod 0555 /opt/pi/pi /usr/local/bin/td /opt/sandbox/launch.py \
    && chown -R agent:agent /home/agent \
    && chmod 0600 /home/agent/.config/pi/agent/*.json
ENV HOME=/home/agent TERM=xterm-256color TD_NO_UPDATE=1 PI_SKIP_VERSION_CHECK=1 PI_OFFLINE=1
WORKDIR /workspace
USER 1000:1000
ENTRYPOINT ["/usr/bin/python3", "/opt/sandbox/launch.py"]

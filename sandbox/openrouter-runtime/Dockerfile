FROM archlinux:base
COPY tools-packages.txt /tmp/tools-packages.txt
RUN pacman -Syu --noconfirm --needed ca-certificates git python ripgrep icu curl $(cat /tmp/tools-packages.txt) && pacman -Scc --noconfirm && rm /tmp/tools-packages.txt
RUN useradd --uid 1000 --create-home --shell /bin/bash agent && install -d -o agent -g agent -m 0700 /home/agent/.config/pi/agent /home/agent/.remote-agent-prompts && install -d -o agent -g agent /workspace /opt/sandbox
COPY pi/ /opt/pi/
COPY launch.py /opt/sandbox/launch.py
COPY settings.json models.json /opt/sandbox/config/
RUN chmod 0555 /opt/pi/pi /opt/sandbox/launch.py && chown -R agent:agent /home/agent
USER 1000:1000
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/python3", "/opt/sandbox/launch.py"]

#!/usr/bin/env python3
"""Relay one guest TCP stream to the fixed OpenRouter proxy."""

import os
import select
import socket
import sys


def relay(sock, input_fd, output_fd):
    reading_input = True
    while True:
        readable, _, _ = select.select(
            [sock, *([input_fd] if reading_input else [])], [], []
        )
        if sock in readable:
            data = sock.recv(65536)
            if not data:
                return
            view = memoryview(data)
            while view:
                view = view[os.write(output_fd, view) :]
        if reading_input and input_fd in readable:
            data = os.read(input_fd, 65536)
            if data:
                sock.sendall(data)
            else:
                reading_input = False
                sock.shutdown(socket.SHUT_WR)


def main():
    try:
        with socket.create_connection(("172.30.253.1", 3128), timeout=10) as sock:
            sock.settimeout(None)
            relay(sock, sys.stdin.fileno(), sys.stdout.fileno())
    except OSError as exc:
        print(f"OpenRouter relay: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

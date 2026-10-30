#!/usr/bin/env python3
"""Relay one guest TCP stream to the fixed OpenRouter proxy."""

import os
import re
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


def read_request(input_fd):
    data = bytearray()
    while True:
        end = data.find(b"\r\n\r\n")
        if end >= 0:
            if end + 4 > 65536:
                raise ValueError("header too large")
            break
        if len(data) >= 65536:
            raise ValueError("header too large")
        chunk = os.read(input_fd, 65536 - len(data))
        if not chunk:
            raise ValueError("incomplete header")
        data.extend(chunk)

    lines = bytes(data[:end]).split(b"\r\n")
    if not lines[0]:
        raise ValueError("invalid request line")
    hosts = []
    for index, line in enumerate(lines[1:], 1):
        name, sep, value = line.partition(b":")
        if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("invalid header")
        if any(c < 32 and c != 9 or c == 127 for c in value):
            raise ValueError("invalid header value")
        if name.lower() == b"host":
            hosts.append(index)
            if value.strip(b" \t") != b"10.0.2.100:3128":
                raise ValueError("invalid host")
    if len(hosts) != 1:
        raise ValueError("invalid host")
    index = hosts[0]
    name, _, value = lines[index].partition(b":")
    lines[index] = name + b":" + value.replace(b"10.0.2.100:3128", b"172.30.253.1:3128")
    return b"\r\n".join(lines) + b"\r\n\r\n" + data[end + 4 :]


def serve(input_fd, output_fd, connect):
    try:
        request = read_request(input_fd)
    except ValueError as exc:
        print(f"OpenRouter relay: {exc}", file=sys.stderr)
        os.write(
            output_fd,
            b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n",
        )
        return 1
    try:
        with connect() as sock:
            sock.settimeout(None)
            sock.sendall(request)
            relay(sock, input_fd, output_fd)
    except OSError:
        print("OpenRouter relay: upstream or I/O failure", file=sys.stderr)
        return 1
    return 0


def main():
    try:
        return serve(
            sys.stdin.fileno(),
            sys.stdout.fileno(),
            lambda: socket.create_connection(("172.30.253.1", 3128), timeout=10),
        )
    except OSError:
        print("OpenRouter relay: input or output failure", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

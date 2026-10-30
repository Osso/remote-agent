#!/usr/bin/env python3
"""Reverse proxy HTTP requests to OpenRouter without logging their contents."""

import argparse
import asyncio
import ipaddress
import re
import ssl


async def connect_upstream():
    return await asyncio.open_connection(
        "openrouter.ai",
        443,
        ssl=ssl.create_default_context(),
        server_hostname="openrouter.ai",
    )


async def copy_exactly(reader, writer, length):
    while length:
        data = await reader.read(min(length, 65536))
        if not data:
            raise asyncio.IncompleteReadError(data, length)
        writer.write(data)
        await writer.drain()
        length -= len(data)


async def copy_chunked(reader, writer):
    while True:
        line = await reader.readuntil(b"\r\n")
        size_text = line[:-2].split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9a-fA-F]+", size_text):
            raise ValueError("invalid chunk size")
        size = int(size_text, 16)
        writer.write(line)
        await writer.drain()
        if not size:
            # Trailers are not forwarded: they can contain hop-by-hop headers.
            while await reader.readuntil(b"\r\n") != b"\r\n":
                pass
            writer.write(b"\r\n")
            await writer.drain()
            return
        await copy_exactly(reader, writer, size)
        if await reader.readexactly(2) != b"\r\n":
            raise ValueError("invalid chunk ending")
        writer.write(b"\r\n")
        await writer.drain()


def parse_request(header, local_address):
    lines = header[:-4].split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3:
        raise ValueError("invalid request line")
    method, target, version = parts
    if (
        not re.fullmatch(rb"[A-Z]+", method)
        or method == b"CONNECT"
        or version != b"HTTP/1.1"
        or not target.startswith(b"/")
        or target.startswith(b"//")
        or any(c <= 32 or c >= 127 for c in target)
        or b"#" in target
        or b"\\" in target
    ):
        raise ValueError("unsupported request")
    headers = []
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("invalid header")
        value = value.strip(b" \t")
        if any(c < 32 and c != 9 or c == 127 for c in value):
            raise ValueError("invalid header value")
        headers.append((name.lower(), value))
    values = lambda name: [value for key, value in headers if key == name]
    host = f"{local_address[0]}:{local_address[1]}".encode()
    if values(b"host") != [host]:
        raise ValueError("invalid host")
    if values(b"upgrade") or values(b"expect"):
        raise ValueError("unsupported request")
    connection = b",".join(values(b"connection")).lower().split(b",")
    connection = {item.strip() for item in connection}
    if b"upgrade" in connection:
        raise ValueError("upgrade not supported")
    lengths = values(b"content-length")
    encodings = values(b"transfer-encoding")
    if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
        raise ValueError("ambiguous framing")
    if lengths and not re.fullmatch(rb"[0-9]+", lengths[0]):
        raise ValueError("invalid length")
    if encodings and encodings[0].lower() != b"chunked":
        raise ValueError("invalid transfer encoding")
    if connection & {b"host", b"content-length", b"transfer-encoding"}:
        raise ValueError("invalid connection header")
    excluded = connection | {
        b"host",
        b"connection",
        b"proxy-connection",
        b"proxy-authorization",
        b"keep-alive",
        b"te",
        b"trailer",
        b"upgrade",
    }
    forwarded = [(name, value) for name, value in headers if name not in excluded]
    request = (
        method
        + b" "
        + target
        + b" HTTP/1.1\r\nHost: openrouter.ai\r\nConnection: close\r\n"
    )
    request += b"".join(name + b": " + value + b"\r\n" for name, value in forwarded)
    return request + b"\r\n", int(lengths[0]) if lengths else 0, bool(encodings)


async def handle_client(reader, writer, connect=connect_upstream):
    upstream_writer = None
    response_started = False
    try:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            request, length, chunked = parse_request(
                header, writer.get_extra_info("sockname")
            )
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            await writer.drain()
            return
        upstream_reader, upstream_writer = await connect()
        upstream_writer.write(request)
        await upstream_writer.drain()
        if chunked:
            await copy_chunked(reader, upstream_writer)
        else:
            await copy_exactly(reader, upstream_writer, length)
        response = await upstream_reader.readuntil(b"\r\n\r\n")
        status = response.split(b"\r\n", 1)[0].split(b" ")
        if (
            len(status) < 2
            or not status[0].startswith(b"HTTP/1.")
            or status[1].startswith(b"1")
        ):
            raise ValueError("unsupported upstream response")
        writer.write(response)
        response_started = True
        await writer.drain()
        while data := await upstream_reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (
        OSError,
        ValueError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        if not response_started:
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
            )
            try:
                await writer.drain()
            except OSError:
                pass
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
        writer.close()


async def serve(host, port):
    address = ipaddress.ip_address(host)
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        raise ValueError("host must be a dedicated bridge address")
    server = await asyncio.start_server(handle_client, host, port)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="dedicated bridge address")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    asyncio.run(serve(args.host, args.port))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Forward CONNECT tunnels to OpenRouter without logging request contents."""

import argparse
import asyncio


async def relay(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        writer.close()


async def handle_client(reader, writer, connect=asyncio.open_connection):
    upstream_writer = None
    try:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        lines = header[:-4].split(b"\r\n")
        if lines[0] not in (
            b"CONNECT openrouter.ai:443 HTTP/1.1",
            b"CONNECT openrouter.ai:443 HTTP/1.0",
        ):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        try:
            upstream_reader, upstream_writer = await connect("openrouter.ai", 443)
        except (OSError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(
            relay(reader, upstream_writer), relay(upstream_reader, writer)
        )
    except (ConnectionError, OSError):
        pass
    finally:
        if upstream_writer is not None:
            upstream_writer.close()
            await upstream_writer.wait_closed()
        writer.close()
        await writer.wait_closed()


async def serve(host, port):
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

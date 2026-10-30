import asyncio
import importlib.util
import unittest
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "openrouter_proxy",
    Path(__file__).resolve().parents[1] / "sandbox/openrouter_proxy.py",
)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = 0
        self.headers = asyncio.Queue()
        self.first_body = asyncio.Queue()
        self.finish_response = asyncio.Event()

        async def upstream(reader, writer):
            try:
                self.connections += 1
                header = await reader.readuntil(b"\r\n\r\n")
                await self.headers.put(header)
                first = await reader.readexactly(5)
                await self.first_body.put(first)
                rest = await reader.readexactly(6)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: "
                    + first
                    + b"\n\n"
                )
                await writer.drain()
                await self.finish_response.wait()
                writer.write(b"data: " + rest + b"\n\n")
                await writer.drain()
            finally:
                writer.close()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = self.upstream.sockets[0].getsockname()[1]

        async def connect():
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        async def handle(reader, writer):
            await proxy.handle_client(reader, writer, connect)

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.host = f"127.0.0.1:{self.port}".encode()

    async def asyncTearDown(self):
        self.finish_response.set()
        self.server.close()
        self.upstream.close()
        await self.server.wait_closed()
        await self.upstream.wait_closed()

    async def request(self, data):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(data)
        await writer.drain()
        return reader, writer

    async def test_streams_request_and_response_to_fixed_host(self):
        reader, writer = await self.request(
            b"POST /api/v1/chat/completions HTTP/1.1\r\nHost: "
            + self.host
            + b"\r\nAuthorization: Bearer test-secret\r\nContent-Length: 11\r\n\r\nfirst"
        )
        header = await asyncio.wait_for(self.headers.get(), 2)
        self.assertIn(b"POST /api/v1/chat/completions HTTP/1.1\r\n", header)
        self.assertIn(b"Host: openrouter.ai\r\n", header)
        self.assertIn(b"authorization: Bearer test-secret\r\n", header)
        self.assertEqual(await asyncio.wait_for(self.first_body.get(), 2), b"first")
        writer.write(b"second")
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\n\n"), 2)
        self.assertIn(b"data: first\n\n", response)
        self.finish_response.set()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"data: second\n\n")
        writer.close()
        await writer.wait_closed()

    async def test_rejects_other_hosts_targets_and_upgrade(self):
        requests = [
            b"GET /api/v1/models HTTP/1.1\r\nHost: evil.test",
            b"GET /api/v1/models HTTP/1.1\r\nHost: openrouter.ai",
            b"GET http://evil.test/api HTTP/1.1\r\nHost: " + self.host,
            b"GET https://openrouter.ai/api HTTP/1.1\r\nHost: " + self.host,
            b"GET //evil.test/api HTTP/1.1\r\nHost: " + self.host,
            b"CONNECT openrouter.ai:443 HTTP/1.1\r\nHost: " + self.host,
            b"GET / HTTP/1.1\r\nHost: " + self.host + b"\r\nUpgrade: websocket",
            b"GET / HTTP/1.1\r\nHost: " + self.host + b"\r\nConnection: Upgrade",
            b"GET / HTTP/1.1\r\nHost: " + self.host + b"\r\nHost: evil.test",
            b"POST / HTTP/1.1\r\nHost: "
            + self.host
            + b"\r\nContent-Length: 1\r\nTransfer-Encoding: chunked",
        ]
        for request in requests:
            with self.subTest(request=request):
                reader, writer = await self.request(request + b"\r\n\r\n")
                self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 400 "))
                writer.close()
                await writer.wait_closed()
        self.assertEqual(self.connections, 0)

    async def test_upstream_failure_returns_bad_gateway(self):
        async def fail():
            raise OSError("connection refused")

        async def handle(reader, writer):
            await proxy.handle_client(reader, writer, fail)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            await writer.drain()
            self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 502 "))
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    unittest.main()

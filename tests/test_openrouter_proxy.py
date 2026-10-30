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
        self.targets = []
        self.received = []

        async def upstream(reader, writer):
            data = await reader.read()
            self.received.append(data)
            writer.write(b"response:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.upstream = await asyncio.start_server(upstream, "127.0.0.1", 0)
        upstream_port = self.upstream.sockets[0].getsockname()[1]

        async def connect(host, port):
            self.targets.append((host, port))
            return await asyncio.open_connection("127.0.0.1", upstream_port)

        async def handle(reader, writer):
            await proxy.handle_client(reader, writer, connect)

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        self.upstream.close()
        await self.server.wait_closed()
        await self.upstream.wait_closed()

    async def request(self, data):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(data)
        await writer.drain()
        return reader, writer

    async def test_tunnel_streams_buffered_data_and_half_close(self):
        reader, writer = await self.request(
            b"CONNECT openrouter.ai:443 HTTP/1.1\r\n"
            b"Proxy-Authorization: secret\r\n\r\nfirst"
        )
        self.assertEqual(
            await reader.readuntil(b"\r\n\r\n"),
            b"HTTP/1.1 200 Connection Established\r\n\r\n",
        )
        writer.write(b" second")
        await writer.drain()
        writer.write_eof()
        self.assertEqual(
            await asyncio.wait_for(reader.read(), 2), b"response:first second"
        )
        self.assertEqual(self.received, [b"first second"])
        self.assertEqual(self.targets, [("openrouter.ai", 443)])
        writer.close()
        await writer.wait_closed()

    async def test_rejects_other_destinations_and_methods(self):
        requests = [
            b"CONNECT example.com:443 HTTP/1.1",
            b"CONNECT openrouter.ai:80 HTTP/1.1",
            b"CONNECT openrouter.ai.evil.test:443 HTTP/1.1",
            b"CONNECT openrouter.ai:443@evil.test:443 HTTP/1.1",
            b"CONNECT OPENROUTER.AI:443 HTTP/1.1",
            b"GET http://openrouter.ai/ HTTP/1.1",
            b"CONNECT openrouter.ai:443 HTTP/1.1 extra",
        ]
        for request in requests:
            with self.subTest(request=request):
                reader, writer = await self.request(request + b"\r\n\r\n")
                self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 403 "))
                writer.close()
                await writer.wait_closed()
        self.assertEqual(self.targets, [])

    async def test_upstream_failure_returns_bad_gateway(self):
        async def fail(host, port):
            raise OSError("connection refused")

        async def handle(reader, writer):
            await proxy.handle_client(reader, writer, fail)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT openrouter.ai:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 502 "))
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    async def test_incomplete_request_does_not_connect(self):
        reader, writer = await self.request(b"CONNECT openrouter.ai:443 HTTP/1.1\r\n")
        writer.write_eof()
        self.assertEqual(await reader.read(), b"")
        self.assertEqual(self.targets, [])
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()

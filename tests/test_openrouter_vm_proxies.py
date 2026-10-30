import http.client
import http.server
import importlib.util
import io
import os
from pathlib import Path
import socket
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1] / "sandbox" / "openrouter-vm"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repo_proxy = load("repo_proxy")
relay = load("relay")
DATA = b"package data" * 10000


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.respond(False)

    def do_GET(self):
        self.respond(True)

    def respond(self, body):
        data = DATA
        if self.headers.get("Range") == "bytes=3-8":
            data = DATA[3:9]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 3-8/{len(DATA)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, raw, factory=None):
        output = io.BytesIO()
        repo_proxy.serve(
            io.BytesIO(raw),
            output,
            factory
            or (
                lambda: http.client.HTTPConnection(
                    *self.server.server_address, timeout=2
                )
            ),
        )
        output.seek(0)
        response = http.client.HTTPResponse(
            type("Socket", (), {"makefile": lambda self, *args: output})()
        )
        response.begin()
        return response

    def test_get_head_and_range(self):
        for method in ("GET", "HEAD"):
            response = self.request(
                f"{method} /core/os/x86_64/core.db HTTP/1.1\r\nHost: 10.0.2.100:3129\r\n\r\n".encode()
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.getheader("Content-Length")), len(DATA))
            if method == "GET":
                self.assertEqual(response.read(), DATA)
        response = self.request(
            b"GET /pkg HTTP/1.1\r\nHost: 10.0.2.100\r\nRange: bytes=3-8\r\n\r\n"
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(response.getheader("Content-Range"), f"bytes 3-8/{len(DATA)}")
        self.assertEqual(response.read(), DATA[3:9])
        self.assertEqual(self.request(b"GET /pkg HTTP/1.0\r\n\r\n").read(), DATA)

    def test_denied_requests(self):
        for target in (
            "http://evil.test/a",
            "//evil.test/a",
            "/a/../b",
            "/%2e%2e/a",
            "/%0d%0aHost:evil",
        ):
            response = self.request(
                f"GET {target} HTTP/1.1\r\nHost: 10.0.2.100\r\n\r\n".encode()
            )
            self.assertEqual(response.status, 400, target)
        for raw in (
            b"GET / HTTP/1.1\r\nHost: evil.test\r\n\r\n",
            b"POST / HTTP/1.1\r\nHost: 10.0.2.100\r\n\r\n",
            b"CONNECT evil.test:443 HTTP/1.1\r\nHost: 10.0.2.100\r\n\r\n",
        ):
            self.assertGreaterEqual(self.request(raw).status, 400)

    def test_upstream_failure(self):
        def fail():
            raise OSError("connection failed")

        self.assertEqual(self.request(b"GET / HTTP/1.0\r\n\r\n", fail).status, 502)

    def test_relay_half_close_and_stream(self):
        left, right = socket.socketpair()
        incoming_r, incoming_w = os.pipe()
        outgoing_r, outgoing_w = os.pipe()
        thread = threading.Thread(
            target=relay.relay, args=(left, incoming_r, outgoing_w)
        )
        thread.start()
        payload = b"guest data" * 5000
        received = []

        def peer():
            while chunk := right.recv(65536):
                received.append(chunk)
            right.sendall(b"upstream reply")
            right.close()

        peer_thread = threading.Thread(target=peer)
        peer_thread.start()
        with os.fdopen(incoming_w, "wb") as stream:
            stream.write(payload)
        self.assertEqual(os.read(outgoing_r, 100), b"upstream reply")
        thread.join(3)
        peer_thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(b"".join(received), payload)
        left.close()
        for fd in (incoming_r, outgoing_r, outgoing_w):
            os.close(fd)


if __name__ == "__main__":
    unittest.main()

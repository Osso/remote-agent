#!/usr/bin/env python3
"""Serve one package-repository HTTP request over stdin/stdout."""

import http.client
import http.server
import sys
from urllib.parse import unquote, urlsplit

UPSTREAM = "geo.mirror.pkgbuild.com"
GUEST_HOSTS = {"10.0.2.100", "10.0.2.100:3129"}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def valid_target(target):
    try:
        parts = urlsplit(target)
    except ValueError:
        return False
    if (
        parts.scheme
        or parts.netloc
        or not target.startswith("/")
        or target.startswith("//")
    ):
        return False
    decoded = unquote(parts.path)
    return (
        not parts.fragment
        and "%" not in decoded
        and "\\" not in decoded
        and not any(ord(c) < 32 or ord(c) == 127 for c in unquote(target))
        and all(part not in (".", "..") for part in decoded.split("/"))
    )


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.rfile, self.wfile = self.server.streams

    def finish(self):
        self.wfile.flush()

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        super().handle_one_request()
        self.close_connection = True

    def do_GET(self):
        self.forward()

    def do_HEAD(self):
        self.forward()

    def forward(self):
        hosts = self.headers.get_all("Host", [])
        if (
            len(hosts) > 1
            or (hosts and hosts[0] not in GUEST_HOSTS)
            or (not hosts and self.request_version != "HTTP/1.0")
            or not valid_target(self.raw_requestline.decode("iso-8859-1").split()[1])
            or self.headers.get("Transfer-Encoding")
            or self.headers.get("Content-Length") not in (None, "0")
        ):
            self.send_error(400, "Invalid repository request")
            return
        headers = {"Host": UPSTREAM, "Connection": "close"}
        for name in ("Range", "If-Range", "If-Modified-Since", "If-None-Match"):
            if name in self.headers:
                headers[name] = self.headers[name]
        conn = None
        started = False
        try:
            conn = self.server.connection_factory()
            conn.request(self.command, self.path, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            connection_tokens = {
                token.strip().lower()
                for value in response.headers.get_all("Connection", [])
                for token in value.split(",")
            }
            for name, value in response.getheaders():
                if (
                    name.lower() not in HOP_HEADERS
                    and name.lower() not in connection_tokens
                ):
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            if self.command != "HEAD":
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            print(f"Repository upstream error: {exc}", file=sys.stderr)
            if not started:
                self.send_error(502, "Repository upstream failure")
        finally:
            if conn is not None:
                conn.close()


def serve(input_stream, output_stream, connection_factory=None):
    class Server:
        streams = (input_stream, output_stream)
        connection_factory = staticmethod(
            connection_factory
            or (lambda: http.client.HTTPSConnection(UPSTREAM, 443, timeout=30))
        )

    Handler(None, ("guest", 0), Server())


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)

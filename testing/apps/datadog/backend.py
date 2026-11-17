#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Datadog's backend as the customer's Agent reaches it, recorded: no Datadog account is needed.

    python3 backend.py --port 8080 --cert cert.pem --key key.pem

The Agent sends trace intake and stats over plain HTTP to its configured URL (DD_APM_DD_URL, DD_DD_URL: this server).
The EVP proxy (LLM Observability, error tracking) and the telemetry proxy only go over HTTPS to <subdomain>.<site>, so
this server is also the Agent's HTTPS proxy (DD_PROXY_HTTPS): it accepts CONNECT, terminates TLS with a self-signed
certificate (the Agent runs with DD_SKIP_SSL_VALIDATION=true) and records the request it carries, with the tunnel's
host. Everything is answered 202 {}. GET /_requests lists what arrived; the Agent's placeholder key arrives in
DD-API-KEY, which is how the harness proves the Agent forwarded with its own key. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

seen: list[dict] = []
lock = threading.Lock()
tls: ssl.SSLContext | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    tunnel: str | None = None

    def log_message(self, *args):
        pass

    def do_CONNECT(self):  # noqa: N802
        target = self.path
        self.send_response(200, "Connection established")
        self.end_headers()
        self.wfile.flush()
        inner = tls.wrap_socket(self.connection, server_side=True)

        class Tunnelled(Handler):
            tunnel = target

        Tunnelled(inner, self.client_address, self.server)
        self.close_connection = True

    def _handle(self):
        parts = urlsplit(self.path)
        if parts.path == "/_requests" and self.tunnel is None:
            with lock:
                out = json.dumps(seen).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                body += self.rfile.read(size)
                self.rfile.readline()
        else:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
        with lock:
            seen.append(
                {
                    "time": time.time(),
                    "tunnel": self.tunnel,
                    "host": self.headers.get("Host"),
                    "method": self.command,
                    "path": parts.path,
                    "query": parts.query,
                    "headers": dict(self.headers.items()),
                    "body_b64": base64.b64encode(body).decode(),
                }
            )
        out = b"{}"
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = _handle  # noqa: N815


def main() -> None:
    global tls
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a test double inside a container
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()

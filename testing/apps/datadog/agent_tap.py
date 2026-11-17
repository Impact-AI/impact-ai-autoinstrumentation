#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""A recording tap in front of the Datadog Agent: it passes every request on unchanged and records it, with the answer.

    python3 agent_tap.py --port 8126 --upstream agent:8126

Every request is forwarded with its method, path, query, headers (less the hop-by-hop ones) and body, and the Agent's
answer goes back as it came. GET /_tap/requests lists what passed, oldest first, in the recording mock's shape
(method, path, query, headers, body_b64), plus the raw header list, whether the body came chunked, and the answer.
POST /_tap/reset forgets. The harness puts one tap between the tracer and the relay and one between the relay and the
Agent: the same request seen on both sides is how it measures the relay's byte difference. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection"}
seen: list[dict] = []
lock = threading.Lock()
upstream: tuple[str, int] = ("127.0.0.1", 8126)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status: int, body: bytes = b"", headers: list[tuple[str, str]] | None = None):
        self.send_response(status)
        for name, value in headers or []:
            if name.lower() not in HOP_BY_HOP and name.lower() != "content-length":
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> tuple[bytes, bool]:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    return data, True
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return (self.rfile.read(length) if length else b""), False

    def _handle(self):
        parts = urlsplit(self.path)
        if parts.path == "/_tap/requests":
            with lock:
                return self._reply(200, json.dumps(seen).encode(), [("Content-Type", "application/json")])
        if parts.path == "/_tap/reset":
            with lock:
                seen.clear()
            return self._reply(200)
        body, chunked = self._body()
        record = {
            "time": time.time(),
            "method": self.command,
            "path": parts.path,
            "query": parts.query,
            "headers": dict(self.headers.items()),
            "rawHeaders": [[name, value] for name, value in self.headers.items()],
            "chunked": chunked,
            "body_b64": base64.b64encode(body).decode(),
        }
        headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_BY_HOP}
        if chunked:
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(*upstream, timeout=30)
        try:
            connection.request(
                self.command, self.path, body=body if body or self.command in ("POST", "PUT") else None, headers=headers
            )
            response = connection.getresponse()
            answer = response.read()
            record["response"] = {
                "status": response.status,
                "headers": [[name, value] for name, value in response.getheaders()],
                "body_b64": base64.b64encode(answer).decode(),
            }
            self._reply(response.status, answer, response.getheaders())
        except OSError as error:
            record["response"] = {"error": type(error).__name__}
            self._reply(502)
        finally:
            connection.close()
            with lock:
                seen.append(record)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle  # noqa: N815


def main() -> None:
    global upstream
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a test double inside a container
    parser.add_argument("--port", type=int, default=8126)
    parser.add_argument("--upstream", required=True, help="host:port of the Agent (or of the relay)")
    args = parser.parse_args()
    host, _, port = args.upstream.rpartition(":")
    upstream = (host, int(port))
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()

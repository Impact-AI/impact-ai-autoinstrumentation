#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""A recording mock of the OTLP trace endpoint a Dynatrace environment documents, for the copy-at-the-exporter harness.

It is built from Dynatrace's public documentation only and runs no Dynatrace software:

  https://docs.dynatrace.com/docs/ingest-from/opentelemetry/otlp-api
  https://docs.dynatrace.com/docs/dynatrace-api/environment-api/opentelemetry/post-traces
  https://docs.dynatrace.com/docs/dynatrace-api/basics/dynatrace-api-authentication

What those pages say, and what this mock therefore does:

  POST /api/v2/otlp/v1/traces              the SaaS environment's address (https://<environment>.live.dynatrace.com)
  POST /e/<environment>/api/v2/otlp/v1/traces    an Environment ActiveGate's address
       /v1/metrics and /v1/logs beside them are recorded and answered 200 (an SDK may send them too)

  - The token travels in the Authorization header: `Api-Token <token>` for a Classic access token, `Bearer <token>` for
    a platform token. Without one of the tokens the mock was started with, the answer is 401.
  - Only HTTP and binary protobuf: a Content-Type other than application/x-protobuf is answered 415, and a body that is
    not a protobuf message is answered 400. gzip is read; another Content-Encoding is answered 415.
  - An accepted request is answered 200 with an empty ExportTraceServiceResponse.

The answers' bodies and the 401 and 415 for an unknown encoding are this mock's own: the documentation names the codes,
not their wording. Like testing/mock/server.py, it records every request and serves the same test control, so that
testing/mock/recording.py turns its capture into a recording:

  GET  /_requests[?path=P&since=N]   every request so far, oldest first (bodies base64)
  POST /_reset                       forget requests and rules
  POST /_control                     {"rules": [{"path": P, "status": 503, "times": 2, "delay": 0}]}
  GET  /healthz

    python3 tenant.py --port 8092 --token dt0c01.PLANTED --token dt0s16.PLANTED

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# The documented trace ingest paths: the SaaS environment's, and an ActiveGate's with the environment in the path.
TRACES = re.compile(r"^(?:/e/[A-Za-z0-9_-]{1,64})?/api/v2/otlp/v1/traces$")
OTHER_SIGNALS = re.compile(r"^(?:/e/[A-Za-z0-9_-]{1,64})?/api/v2/otlp/v1/(?:metrics|logs)$")
SCHEMES = ("Api-Token", "Bearer")
PROTOBUF = "application/x-protobuf"
MAX_BODY = 64 << 20

state_lock = threading.Lock()
requests_seen: list[dict] = []
rules: list[dict] = []
tokens: set[str] = set()


def token_of(header: str | None) -> str | None:
    """The token of an Authorization header in one of the documented schemes, or None."""
    scheme, _, token = (header or "").strip().partition(" ")
    token = token.strip()
    return token if scheme in SCHEMES and token else None


def media_type(header: str | None) -> str:
    return (header or "").split(";")[0].strip().lower()


def varint(data: bytes, at: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if at >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def is_protobuf_message(data: bytes) -> bool:
    """Whether `data` reads as the fields of one protobuf message (the wire format, not the schema)."""
    at = 0
    try:
        while at < len(data):
            key, at = varint(data, at)
            field, wire = key >> 3, key & 7
            if field == 0:
                return False
            if wire == 0:
                _, at = varint(data, at)
            elif wire == 1:
                at += 8
            elif wire == 2:
                length, at = varint(data, at)
                at += length
            elif wire == 5:
                at += 4
            else:
                return False
            if at > len(data):
                return False
    except ValueError:
        return False
    return True


def answer(path: str, headers, body: bytes) -> int:
    """The status the documented endpoint gives a request (before any test rule)."""
    if not (TRACES.match(path) or OTHER_SIGNALS.match(path)):
        return 404
    if token_of(headers.get("Authorization")) not in tokens:
        return 401
    if media_type(headers.get("Content-Type")) != PROTOBUF:
        return 415
    encoding = (headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return 400
    elif encoding != "identity":
        return 415
    if len(body) > MAX_BODY:
        return 413
    return 200 if is_protobuf_message(body) else 400


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            # Node.js exporters send their body in chunks.
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):  # trailers
                        pass
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/healthz":
            return self._send(200, b'{"ok":true}')
        if parts.path == "/_requests":
            query = parse_qs(parts.query)
            wanted = query.get("path", [None])[0]
            since = int(query.get("since", ["0"])[0])
            with state_lock:
                out = [r for r in requests_seen if r["seq"] > since and (wanted is None or r["path"] == wanted)]
            return self._send(200, json.dumps(out).encode())
        return self._send(404, b'{"error":{"code":404,"message":"Not found"}}')

    def do_POST(self):  # noqa: N802
        body = self._body()
        parts = urlsplit(self.path)
        if parts.path == "/_reset":
            with state_lock:
                requests_seen.clear()
                rules.clear()
            return self._send(200, b"{}")
        if parts.path == "/_control":
            with state_lock:
                rules[:] = [dict(rule, _left=rule.get("times")) for rule in json.loads(body or b"{}").get("rules", [])]
            return self._send(200, b"{}")
        with state_lock:
            record = {
                "seq": len(requests_seen) + 1,
                "time": time.time(),
                "method": self.command,
                "path": parts.path,
                "query": parts.query,
                "headers": dict(self.headers.items()),
                "body_b64": base64.b64encode(body).decode(),
            }
            requests_seen.append(record)
            matched = next((r for r in rules if r["path"] == parts.path and r.get("_left") != 0), None)
            if matched is not None and matched.get("_left") is not None:
                matched["_left"] -= 1
        status = answer(parts.path, self.headers, body)
        if matched is not None and status == 200:
            time.sleep(float(matched.get("delay", 0)))
            status = int(matched.get("status", 200))
        with state_lock:
            record["status"] = status
        if status == 200:
            return self._send(200, b"", PROTOBUF)
        self._send(status, json.dumps({"error": {"code": status, "message": "Refused by the mock"}}).encode())

    do_PUT = do_POST  # noqa: N815


def server(host: str, port: int, accepted: list[str]) -> ThreadingHTTPServer:
    """The mock, accepting these tokens, not yet serving (its port is server_address[1])."""
    with state_lock:
        tokens.clear()
        tokens.update(accepted)
        requests_seen.clear()
        rules.clear()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--token", action="append", default=[], help="a token the Authorization header may carry")
    args = parser.parse_args()
    server(args.host, args.port, args.token).serve_forever()


if __name__ == "__main__":
    main()

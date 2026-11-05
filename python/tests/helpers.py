# SPDX-License-Identifier: Apache-2.0

"""Test helpers shared by the unit tests."""

from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Sink:
    """A local OTLP/HTTP receiver that records requests and answers with scripted statuses."""

    def __init__(self, statuses=None):
        self.requests: list[dict] = []
        self.statuses = list(statuses or [])
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                sink.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status = sink.statuses.pop(0) if sink.statuses else 200
                self.send_response(status)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def spans(self):
        out = []
        for r in self.requests:
            payload = json.loads(
                gzip.decompress(r["body"]) if r["headers"].get("Content-Encoding") == "gzip" else r["body"]
            )
            for rs in payload["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    out.extend(ss["spans"])
        return out

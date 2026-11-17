#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""A recording double of New Relic's collector, for New Relic's open-source APM agents.

The agents speak New Relic's agent protocol over HTTPS only: every call is a POST to
/agent_listener/invoke_raw_method?method=<name>&license_key=...&marshal_format=json&protocol_version=17, with a JSON
body, gzip or deflate compressed above a size. The double answers as the collector does, so that an agent connects and
sends what it collected:

  preconnect      {"return_value": {"redirect_host": <--redirect-host, or the host the agent called>}}
  connect         {"return_value": {"agent_run_id": ..., the collect_* switches on, AI monitoring on, harvest limits}}
  anything else   {"return_value": null}

and records every request, as the kit's recording mock (testing/mock/server.py) does:

  GET /_requests[?since=N]   every request so far, oldest first: seq, method, path, query, headers, body_b64
  POST /_reset               forget them
  GET /healthz

testing/mock/recording.py turns what /_requests answers into a recording for the relay's decoder tests. The double
holds no credential and checks none: the license key the agent sends is recorded like any other query parameter, and
a recording keeps only the methods asked for (testing/apps/newrelic_record.py). Standard library only.

    python3 testing/apps/newrelic_collector.py --port 8443 --cert leaf.pem --key leaf.key
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PROTOCOL_PATH = "/agent_listener/invoke_raw_method"
RUN_ID = "recording-run-1"

# What the collector answers connect with: the agent's run id, every kind of data switched on, AI monitoring on (an
# account's collect_ai), and the harvest limits the agents expect. Span and custom events are what the relay decodes.
CONNECT = {
    "agent_run_id": RUN_ID,
    "account_id": "1",
    "application_id": "1",
    "primary_application_id": "1",
    "trusted_account_key": "1",
    "entity_guid": "MXxBUE18QVBQTElDQVRJT058MQ",
    "collect_analytics_events": True,
    "collect_custom_events": True,
    "collect_span_events": True,
    "collect_error_events": True,
    "collect_errors": True,
    "collect_traces": True,
    "collect_ai": True,
    "apdex_t": 0.5,
    "data_report_period": 60,
    "sampling_target": 10,
    "sampling_target_period_in_seconds": 60,
    "max_payload_size_in_bytes": 1000000,
    "event_harvest_config": {
        "report_period_ms": 5000,
        "harvest_limits": {
            "analytic_event_data": 1200,
            "custom_event_data": 3600,
            "error_event_data": 100,
            "log_event_data": 833,
        },
    },
    "span_event_harvest_config": {"report_period_ms": 60000, "harvest_limit": 2000},
}

lock = threading.Lock()
seen: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    redirect_host: str | None = None

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
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
            since = int(parse_qs(parts.query).get("since", ["0"])[0])
            with lock:
                out = [r for r in seen if r["seq"] > since]
            return self._send(200, json.dumps(out).encode())
        return self._send(404, b"{}")

    def do_POST(self):  # noqa: N802
        self._handle()

    do_PUT = do_POST  # noqa: N815

    def _handle(self):
        body = self._body()
        parts = urlsplit(self.path)
        if parts.path == "/_reset":
            with lock:
                seen.clear()
            return self._send(200, b"{}")
        with lock:
            seen.append(
                {
                    "seq": len(seen) + 1,
                    "time": time.time(),
                    "method": self.command,
                    "path": parts.path,
                    "query": parts.query,
                    "headers": dict(self.headers.items()),
                    "body_b64": base64.b64encode(body).decode(),
                }
            )
        if parts.path != PROTOCOL_PATH:
            return self._send(404, b"{}")
        method = parse_qs(parts.query).get("method", [""])[0]
        if method == "preconnect":
            host = self.redirect_host or (self.headers.get("Host") or "").rsplit(":", 1)[0]
            # The Node.js agent takes the port from redirect_host and uses 443 without one; the Python and Java agents
            # keep their own port setting. The real collector answers a host alone, on 443.
            if (self.headers.get("User-Agent") or "").startswith("NewRelic-NodeAgent"):
                host += f":{self.server.server_address[1]}"
            answer = {"redirect_host": host}
        elif method == "connect":
            answer = CONNECT
        else:
            answer = None
        self._send(200, json.dumps({"return_value": answer}).encode())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert", required=True, help="the server certificate (PEM), with its chain")
    parser.add_argument("--key", required=True, help="its private key (PEM)")
    parser.add_argument("--redirect-host", help="what preconnect answers; the host the agent called when omitted")
    args = parser.parse_args()
    Handler.redirect_host = args.redirect_host
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

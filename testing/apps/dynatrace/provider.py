#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The model provider of the canonical interaction, for recording what an instrumentation exports of it.

    python3 provider.py --port 8091

POST /v1/chat/completions answers as OpenAI's chat API does, with the interaction's two turns: a request without a tool
result asks for the tool `get_order` (120 input and 20 output tokens, finish `tool_calls`); a request with the tool's
result answers in text (300 input and 80 output tokens, finish `stop`). Both answer as `gpt-4o-2024-08-06`, and their
content carries the planted text CONTENT-CANARY. The kit's recording mock answers every call alike (7 and 5 tokens),
which does not add up to the interaction; this answers it exactly. Standard library only.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANARY = "CONTENT-CANARY"
MODEL = "gpt-4o-2024-08-06"


def answer(request: dict) -> dict:
    """The chat completion for one turn of the interaction."""
    messages = request.get("messages") or []
    if not any(isinstance(message, dict) and message.get("role") == "tool" for message in messages):
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": json.dumps({"order_id": f"1042 {CANARY}"})},
                }
            ],
        }
        finish, usage, answer_id = "tool_calls", (120, 20), "chatcmpl-canonical-1"
    else:
        message = {"role": "assistant", "content": f"Order 1042 has shipped. {CANARY}"}
        finish, usage, answer_id = "stop", (300, 80), "chatcmpl-canonical-2"
    return {
        "id": answer_id,
        "object": "chat.completion",
        "created": 1762646400,
        "model": MODEL,
        "system_fingerprint": "fp_canonical",
        "choices": [{"index": 0, "finish_reason": finish, "message": message, "logprobs": None}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._send(200, {"ok": True})

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

    def do_POST(self):  # noqa: N802
        body = self._body() or b"{}"
        if not self.path.split("?")[0].endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "not a chat completion"}})
        try:
            request = json.loads(body)
        except ValueError:
            return self._send(400, {"error": {"message": "not JSON"}})
        self._send(200, answer(request if isinstance(request, dict) else {}))


def server(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    server(args.host, args.port).serve_forever()


if __name__ == "__main__":
    main()

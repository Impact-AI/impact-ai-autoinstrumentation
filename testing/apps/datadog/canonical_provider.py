#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The model provider of the canonical interaction, for recording a tracer's view of it.

    python3 canonical_provider.py --port 8091

POST /v1/chat/completions answers as OpenAI's chat API does, with the canonical interaction's two turns: a request
without a tool result asks for the tool `get_order` (120 input and 20 output tokens, finish `tool_calls`); a request
with the tool's result answers in text (300 input and 80 output tokens, finish `stop`). Both answer as
`gpt-4o-2024-08-06`, and their content carries the planted text CONTENT-CANARY. The kit's recording mock answers every
call alike (7 and 5 tokens), which does not add up to the interaction; this answers it exactly. Standard library only.
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
    if not any(message.get("role") == "tool" for message in messages):
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
        finish, usage = "tool_calls", (120, 20)
    else:
        message = {"role": "assistant", "content": f"Order 1042 has shipped. {CANARY}"}
        finish, usage = "stop", (300, 80)
    return {
        "id": "chatcmpl-canonical-1" if finish == "tool_calls" else "chatcmpl-canonical-2",
        "object": "chat.completion",
        "created": 1762646400,
        "model": MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "message": message, "logprobs": None}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path.rstrip("/").endswith("/chat/completions"):
            status, body = 200, json.dumps(answer(request)).encode()
        else:
            status, body = 404, b'{"error":"not the canonical interaction"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()

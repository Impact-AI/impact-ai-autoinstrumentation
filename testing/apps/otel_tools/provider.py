#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The model provider of the canonical interaction, for recording what an AI tool's SDK writes about it.

    python3 provider.py --port 8091

POST /v1/chat/completions answers as OpenAI's chat API does, with the interaction's two turns: a request without a
tool result asks for the tool `get_order` (120 input and 20 output tokens, finish `tool_calls`); a request with the
tool's result answers in text (300 input and 80 output tokens, finish `stop`). Both answer as `gpt-4o-2024-08-06` and
carry the planted text CONTENT-CANARY. The kit's recording mock answers every call alike (7 and 5 tokens), which does
not add up to the interaction (420 input and 100 output tokens); this answers it exactly. Standard library only.

With --tool-turns N the model asks for the tool N times with the same arguments, once per turn,
before it answers, as an agent does whose prompt makes it run away (the OpenTelemetry demo's aiRunawayAgent flag): a
loop, which is not the canonical interaction.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CANARY = "CONTENT-CANARY"
MODEL = "gpt-4o-2024-08-06"
TURNS = {
    "tool_calls": ("chatcmpl-canonical-1", 120, 20),
    "stop": ("chatcmpl-canonical-2", 300, 80),
}


def answer(request: dict, tool_turns: int = 1) -> dict:
    """The chat completion of one turn of the interaction: the tool until it answered `tool_turns` times, then text."""
    messages = request.get("messages") or []
    answered = sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "tool")
    if answered >= tool_turns:
        finish = "stop"
        message = {"role": "assistant", "content": f"Order 1042 has shipped. {CANARY}", "refusal": None}
    else:
        finish = "tool_calls"
        arguments = json.dumps({"order_id": f"1042 {CANARY}"}, separators=(",", ":"))
        message = {
            "role": "assistant",
            "content": None,
            "refusal": None,
            "tool_calls": [
                {
                    "id": f"call_{answered + 1}",
                    "type": "function",
                    "function": {"name": "get_order", "arguments": arguments},
                }
            ],
        }
    identifier, prompt, completion = TURNS[finish]
    return {
        "id": identifier,
        "object": "chat.completion",
        "created": 1762646400,
        "model": MODEL,
        "system_fingerprint": "fp_canonical",
        "choices": [{"index": 0, "finish_reason": finish, "message": message, "logprobs": None}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    tool_turns = 1

    def log_message(self, *args):
        pass

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.reply(200 if self.path == "/healthz" else 404, b'{"ok":true}')

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path.rstrip("/").endswith("/chat/completions"):
            self.reply(200, json.dumps(answer(request, self.tool_turns)).encode())
        else:
            self.reply(404, b'{"error":"not the canonical interaction"}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--tool-turns", type=int, default=1, help="ask for the tool this many times before answering")
    args = parser.parse_args()
    Handler.tool_turns = args.tool_turns
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()

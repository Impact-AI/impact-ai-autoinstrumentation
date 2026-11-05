# SPDX-License-Identifier: Apache-2.0

"""Fixture workload (WSGI): a Flask app that calls model providers and has no tracing of its own."""

import os

from flask import Flask, jsonify, request

app = Flask(__name__)
STUB = os.environ["STUB_URL"]


def prompt() -> str:
    return (request.get_json(silent=True) or {}).get("prompt", "hi there")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.post("/chat/openai")
def chat_openai():
    from openai import OpenAI

    client = OpenAI(api_key="sk-test", base_url=STUB + "/v1")
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": prompt()}],
    )
    return jsonify(answer=reply.choices[0].message.content)


@app.post("/chat/anthropic")
def chat_anthropic():
    import anthropic

    client = anthropic.Anthropic(api_key="sk-ant-test", base_url=STUB)
    reply = client.messages.create(
        model="claude-stub", max_tokens=64, system="be brief", messages=[{"role": "user", "content": prompt()}]
    )
    return jsonify(answer=reply.content[0].text)


@app.post("/chat/bedrock")
def chat_bedrock():
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1", endpoint_url=STUB)
    reply = client.converse(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        system=[{"text": "be brief"}],
        messages=[{"role": "user", "content": [{"text": prompt()}]}],
    )
    return jsonify(answer=reply["output"]["message"]["content"][0]["text"])


@app.post("/chat/nova")
def chat_nova():
    """Amazon Nova Micro through Converse, the way a returns assistant calls it."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1", endpoint_url=STUB)
    reply = client.converse(
        modelId="amazon.nova-micro-v1:0",
        system=[{"text": "be brief"}],
        messages=[{"role": "user", "content": [{"text": prompt()}]}],
        inferenceConfig={"maxTokens": 64, "temperature": 0.0},
    )
    return jsonify(answer=reply["output"]["message"]["content"][0]["text"])


@app.get("/debug/runtime")
def debug_runtime():
    import importlib
    import sys

    files = {}
    for name in ("typing_extensions", "packaging", "wrapt", "asgiref"):
        try:
            files[name] = getattr(importlib.import_module(name), "__file__", None)
        except ImportError:
            files[name] = None
    return jsonify(
        pythonpath=os.environ.get("PYTHONPATH"),
        sys_path=sys.path,
        files=files,
        wrapt_c="wrapt._wrappers" in sys.modules,
        otel_modules=sorted(m for m in sys.modules if m.startswith("opentelemetry.sdk")),
        python=sys.version.split()[0],
    )

# SPDX-License-Identifier: Apache-2.0

"""Fixture workload (ASGI): a FastAPI app that calls model providers and has no tracing of its own."""

import os

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
STUB = os.environ["STUB_URL"]


class Ask(BaseModel):
    prompt: str = "hi there"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/chat/openai")
def chat_openai(ask: Ask):
    from openai import OpenAI

    client = OpenAI(api_key="sk-test", base_url=STUB + "/v1")
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": ask.prompt}],
    )
    return {"answer": reply.choices[0].message.content}


@app.post("/chat/anthropic")
def chat_anthropic(ask: Ask):
    import anthropic

    client = anthropic.Anthropic(api_key="sk-ant-test", base_url=STUB)
    reply = client.messages.create(
        model="claude-stub", max_tokens=64, system="be brief", messages=[{"role": "user", "content": ask.prompt}]
    )
    return {"answer": reply.content[0].text}


@app.post("/chat/bedrock")
def chat_bedrock(ask: Ask):
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1", endpoint_url=STUB)
    reply = client.converse(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        system=[{"text": "be brief"}],
        messages=[{"role": "user", "content": [{"text": ask.prompt}]}],
    )
    return {"answer": reply["output"]["message"]["content"][0]["text"]}


@app.post("/chat/nova")
def chat_nova(ask: Ask):
    """Amazon Nova Micro through Converse, the way a returns assistant calls it."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name="us-east-1", endpoint_url=STUB)
    reply = client.converse(
        modelId="amazon.nova-micro-v1:0",
        system=[{"text": "be brief"}],
        messages=[{"role": "user", "content": [{"text": ask.prompt}]}],
        inferenceConfig={"maxTokens": 64, "temperature": 0.0},
    )
    return {"answer": reply["output"]["message"]["content"][0]["text"]}


@app.get("/debug/runtime")
def debug_runtime():
    """What the process sees: for the tests that check we leave the application's environment alone."""
    import importlib
    import sys

    files = {}
    for name in ("typing_extensions", "packaging", "wrapt", "asgiref"):
        try:
            files[name] = getattr(importlib.import_module(name), "__file__", None)
        except ImportError:
            files[name] = None
    return {
        "pythonpath": os.environ.get("PYTHONPATH"),
        "sys_path": sys.path,
        "files": files,
        "wrapt_c": "wrapt._wrappers" in sys.modules,
        "otel_modules": sorted(m for m in sys.modules if m.startswith("opentelemetry.sdk")),
        "python": sys.version.split()[0],
    }

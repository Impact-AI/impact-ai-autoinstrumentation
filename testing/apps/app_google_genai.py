# SPDX-License-Identifier: Apache-2.0

"""Fixture workload (ASGI): a FastAPI app that calls Gemini through Google's SDK, google-genai, and has no tracing of
its own.

POST /chat/google-genai/<api>/<call>: api is `gemini` (the Gemini API, with an API key) or `vertex` (Vertex AI, with a
project, a location and Google credentials); call is `sync`, `async`, `stream` or `async-stream`. The mock provider
(testing/mock/server.py) answers both APIs, so no key or credential of Google's is needed: the credentials are an access
token that the SDK takes as it is. The app makes one client per API on first use and keeps it, as an application does:
google-genai closes a client's connections when the client is collected. It imports the SDK when it starts, as an
application does, so that the overhead measurement counts the instrumentation's start in the start time.
"""

import os

from fastapi import FastAPI, HTTPException
from google import genai
from google.genai import types
from pydantic import BaseModel

app = FastAPI()
STUB = os.environ["STUB_URL"]
MODEL = "gemini-2.5-flash"
CLIENTS = {}


class Ask(BaseModel):
    prompt: str = "hi there"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/otel/global-tracer-provider")
def global_tracer_provider():
    """The process's global tracer provider, as a library that makes its spans with the global tracer sees it: the
    package never sets it (the cells of the Python qualification check it after the model calls). Beside it, the GenAI
    log-event switch of the process's environment, which Google's instrumentation sets while it instruments and the
    package puts back: this workload sets none, so none is there."""
    emit_event = os.environ.get("OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT")
    try:
        from opentelemetry import trace
    except ImportError:
        return {"provider": None, "genaiEmitEvent": emit_event}
    return {"provider": type(trace.get_tracer_provider()).__name__, "genaiEmitEvent": emit_event}


def client(api: str):
    """The client of an API, made once and kept."""
    if api not in CLIENTS:
        options = types.HttpOptions(base_url=STUB)
        if api == "gemini":
            CLIENTS[api] = genai.Client(api_key="gemini-stub-key", http_options=options)
        elif api == "vertex":
            from google.oauth2.credentials import Credentials

            CLIENTS[api] = genai.Client(
                vertexai=True,
                project="stub-project",
                location="us-central1",
                credentials=Credentials(token="stub-token"),  # noqa: S106 - the mock takes any token
                http_options=options,
            )
        else:
            raise HTTPException(status_code=404, detail="the API is gemini or vertex")
    return CLIENTS[api]


def config():
    return types.GenerateContentConfig(system_instruction="be brief")


@app.post("/chat/google-genai/{api}/sync")
def generate(api: str, ask: Ask):
    reply = client(api).models.generate_content(model=MODEL, contents=ask.prompt, config=config())
    return {"answer": reply.text}


@app.post("/chat/google-genai/{api}/async")
async def generate_async(api: str, ask: Ask):
    reply = await client(api).aio.models.generate_content(model=MODEL, contents=ask.prompt, config=config())
    return {"answer": reply.text}


@app.post("/chat/google-genai/{api}/stream")
def generate_stream(api: str, ask: Ask):
    chunks = client(api).models.generate_content_stream(model=MODEL, contents=ask.prompt, config=config())
    return {"answer": "".join(chunk.text or "" for chunk in chunks)}


@app.post("/chat/google-genai/{api}/async-stream")
async def generate_async_stream(api: str, ask: Ask):
    chunks = await client(api).aio.models.generate_content_stream(model=MODEL, contents=ask.prompt, config=config())
    return {"answer": "".join([chunk.text or "" async for chunk in chunks])}

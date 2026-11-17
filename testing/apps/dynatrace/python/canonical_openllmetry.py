# SPDX-License-Identifier: Apache-2.0

"""The canonical interaction, traced by OpenLLMetry (the Traceloop SDK) and exported as OTLP to a Dynatrace environment,
configured in code as Dynatrace's OpenLLMetry guide shows: `Traceloop.init(api_endpoint=..., headers=...)`, the base URL
ending in /api/v2/otlp and the token in the Authorization header.

An agent `support` asks `gpt-4o` where an order is; the model asks for the tool `get_order`; the tool runs; a search of
the vector database collection `help-centre` asks for 5 documents and finds three; the model answers. Content carries
CONTENT-CANARY. OpenLLMetry's vector database instrumentation records the top-k, not how many documents came back.

    DT_ENDPOINT=https://<environment>.live.dynatrace.com DT_API_TOKEN=<token> DT_TOKEN_SCHEME=Api-Token \\
    OPENAI_BASE_URL=http://<provider>/v1 python canonical_openllmetry.py

DT_ENDPOINT and DT_API_TOKEN are the names Dynatrace's own samples use; DT_TOKEN_SCHEME is this harness's (`Api-Token`
for a Classic access token, `Bearer` for a platform token). The SDK lets TRACELOOP_BASE_URL and TRACELOOP_HEADERS
replace what the code passes. The collection is an in-memory Qdrant, filled before tracing starts. The OpenAI key is a
planted, fictitious one.
"""

import json
import os

from qdrant_client import QdrantClient, models

CANARY = "CONTENT-CANARY"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up an order",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        },
    }
]
DOCUMENTS = [
    (112, [1.0, 0.0, 0.0], f"Orders ship within two days. {CANARY}"),
    (407, [0.0, 1.0, 0.0], f"Track an order from its page. {CANARY}"),
    (19, [0.0, 0.0, 1.0], f"Return an order within 30 days. {CANARY}"),
]

# Filled before Traceloop starts, so that only the search is part of the traced interaction.
qdrant = QdrantClient(":memory:")
qdrant.create_collection("help-centre", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
qdrant.upsert(
    "help-centre",
    points=[models.PointStruct(id=pid, vector=vector, payload={"text": text}) for pid, vector, text in DOCUMENTS],
)

import openai  # noqa: E402
from traceloop.sdk import Traceloop  # noqa: E402
from traceloop.sdk.decorators import agent, tool  # noqa: E402

Traceloop.init(
    app_name="support-assistant",
    api_endpoint=f"{os.environ['DT_ENDPOINT']}/api/v2/otlp",
    headers={"Authorization": f"{os.environ.get('DT_TOKEN_SCHEME', 'Api-Token')} {os.environ['DT_API_TOKEN']}"},
    resource_attributes={"service.version": "4.2.0"},
    disable_batch=True,
    telemetry_enabled=False,
)


@tool(name="get_order")
def get_order(order_id: str) -> dict:
    return {"status": "shipped", "note": CANARY}


def search_help_centre() -> list:
    return qdrant.query_points("help-centre", query=[0.6, 0.5, 0.4], limit=5).points


@agent(name="support")
def support() -> str:
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "planted-openai-key"))
    user = {"role": "user", "content": f"Where is order 1042? {CANARY}"}
    first = client.chat.completions.create(model="gpt-4o", messages=[user], tools=TOOLS)
    call = first.choices[0].message.tool_calls[0]
    result = get_order(json.loads(call.function.arguments)["order_id"])
    search_help_centre()
    last = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            user,
            first.choices[0].message.model_dump(exclude_none=True),
            {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)},
        ],
        tools=TOOLS,
    )
    return last.choices[0].message.content


if __name__ == "__main__":
    support()
    Traceloop.flush() if hasattr(Traceloop, "flush") else None

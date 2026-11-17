# SPDX-License-Identifier: Apache-2.0

"""The canonical interaction, traced by Datadog's Python tracer (ddtrace) with LLM Observability on.

An agent `support` asks `gpt-4o` where an order is; the model asks for the tool `get_order`; the tool runs; a retrieval
from `help-centre` returns three documents of a top-k of 5; the model answers. Content carries CONTENT-CANARY.

    DD_TRACE_AGENT_URL=http://<agent or relay> DD_LLMOBS_ENABLED=1 DD_LLMOBS_ML_APP=support-assistant \\
    DD_SERVICE=support-assistant DD_VERSION=4.2.0 OPENAI_BASE_URL=http://<canonical_provider>/openai/v1 \\
    python canonical.py

Without DD_LLMOBS_ENABLED the same code runs with APM tracing alone (the OpenAI integration's spans). ddtrace names the
model provider from the client's base URL ("openai" when it contains "openai"), hence the provider's /openai/v1 path.
The OpenAI key is a planted, fictitious one.
"""

import ddtrace.auto  # noqa: F401  # isort: skip

import contextlib
import json
import os

import openai
from ddtrace.llmobs import LLMObs

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
    {"id": "kb-112", "score": 0.91, "name": "Shipping times", "text": f"Orders ship within two days. {CANARY}"},
    {"id": "kb-407", "score": 0.73, "name": "Tracking", "text": f"Track an order from its page. {CANARY}"},
    {"id": "kb-019", "score": 0.52, "name": "Returns", "text": f"Return an order within 30 days. {CANARY}"},
]


def step(kind, name: str):
    """An LLM Observability span of this kind, or nothing when only APM tracing is on."""
    return getattr(LLMObs, kind)(name=name) if LLMObs.enabled else contextlib.nullcontext()


def annotate(span, **data) -> None:
    if LLMObs.enabled:
        LLMObs.annotate(span=span, **data)


def get_order(order_id: str) -> dict:
    with step("tool", "get_order") as span:
        result = {"status": "shipped", "note": CANARY}
        annotate(span, input_data={"order_id": order_id}, output_data=result)
        return result


def search_help_centre(query: str) -> list:
    with step("retrieval", "help-centre") as span:
        annotate(span, input_data=query, output_data=DOCUMENTS, metadata={"top_k": 5})
        return DOCUMENTS


def main() -> None:
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "planted-openai-key"))
    user = {"role": "user", "content": f"Where is order 1042? {CANARY}"}
    with step("agent", "support") as agent:
        first = client.chat.completions.create(model="gpt-4o", messages=[user], tools=TOOLS)
        call = first.choices[0].message.tool_calls[0]
        result = get_order(json.loads(call.function.arguments)["order_id"])
        search_help_centre(f"When does an order ship? {CANARY}")
        asked = {"role": "assistant", "content": None, "tool_calls": [call.model_dump()]}
        answered = {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
        second = client.chat.completions.create(model="gpt-4o", messages=[user, asked, answered], tools=TOOLS)
        annotate(agent, input_data=user["content"], output_data=second.choices[0].message.content)
    if LLMObs.enabled:
        LLMObs.flush()


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: Apache-2.0

"""The canonical interaction, traced by the OpenTelemetry GenAI instrumentation and exported as OTLP to a Dynatrace
environment, configured as Dynatrace's documentation shows (environment variables of the OpenTelemetry SDK).

An agent `support` asks `gpt-4o` where an order is; the model asks for the tool `get_order`; the tool runs; a retrieval
from `help-centre` returns three documents of a top-k of 5; the model answers. Content carries CONTENT-CANARY.

    OTEL_EXPORTER_OTLP_ENDPOINT=https://<environment>.live.dynatrace.com/api/v2/otlp \\
    OTEL_EXPORTER_OTLP_HEADERS="Authorization=Api-Token%20<token>" OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \\
    OTEL_SERVICE_NAME=support-assistant OPENAI_BASE_URL=http://<provider>/v1 python canonical_otel_genai.py

The chats are the OpenAI instrumentation's; the agent, tool and retrieval spans are written with the same package's
GenAI utilities, as an application does for steps no library instruments. The OpenAI key is a planted, fictitious one.
"""

import os

os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")

import json  # noqa: E402

import openai  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: E402
from opentelemetry.util.genai.handler import get_telemetry_handler  # noqa: E402
from opentelemetry.util.genai.types import RetrievalDocument  # noqa: E402

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
# Ids and scores only: the conventions keep a retrieved document's text out of gen_ai.retrieval.documents.
DOCUMENTS = [("kb-112", 0.91), ("kb-407", 0.73), ("kb-019", 0.52)]

# The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS, the documented configuration.
provider = TracerProvider(resource=Resource.create({"service.version": "4.2.0"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
OpenAIInstrumentor().instrument(tracer_provider=provider)
handler = get_telemetry_handler(tracer_provider=provider)


def get_order(order_id: str, call_id: str) -> dict:
    with handler.tool("get_order", agent_name="support") as tool:
        tool.tool_call_id = call_id
        tool.arguments = {"order_id": order_id}
        result = {"status": "shipped", "note": CANARY}
        tool.tool_result = result
        return result


def search_help_centre(query: str) -> list:
    with handler.retrieval(data_source_id="help-centre") as retrieval:
        retrieval.top_k = 5
        retrieval.query_text = query
        retrieval.documents = [RetrievalDocument(id=name, score=score) for name, score in DOCUMENTS]
        return DOCUMENTS


def main() -> None:
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "planted-openai-key"))
    user = {"role": "user", "content": f"Where is order 1042? {CANARY}"}
    with handler.invoke_local_agent(agent_name="support"):
        first = client.chat.completions.create(model="gpt-4o", messages=[user], tools=TOOLS)
        call = first.choices[0].message.tool_calls[0]
        result = get_order(json.loads(call.function.arguments)["order_id"], call.id)
        search_help_centre(f"When does an order ship? {CANARY}")
        client.chat.completions.create(
            model="gpt-4o",
            messages=[
                user,
                first.choices[0].message.model_dump(exclude_none=True),
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)},
            ],
            tools=TOOLS,
        )
    provider.shutdown()


if __name__ == "__main__":
    main()

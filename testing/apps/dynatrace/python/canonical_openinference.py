# SPDX-License-Identifier: Apache-2.0

"""The canonical interaction, traced by OpenInference and exported as OTLP to a Dynatrace environment, configured as
Dynatrace's OpenInference guide shows: an OTLP/HTTP exporter whose endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT and
whose Authorization header is made from DT_API_TOKEN in code, with OPENINFERENCE_ENABLE_GENAI_SEMCONV=true.

An agent `support` asks `gpt-4o` where an order is; the model asks for the tool `get_order`; the tool runs; a retrieval
from `help-centre` returns three documents; the model answers. Content carries CONTENT-CANARY. OpenInference has no key
for a retrieval's top-k.

    OTEL_EXPORTER_OTLP_ENDPOINT=https://<environment>.live.dynatrace.com/api/v2/otlp/v1/traces \\
    DT_API_TOKEN=<token> OTEL_SERVICE_NAME=support-assistant \\
    OPENAI_BASE_URL=http://<provider>/v1 python canonical_openinference.py

DT_TOKEN_SCHEME is this harness's own (default `Bearer`): `Bearer` for a platform token (the guide's first tab),
`Api-Token` for a Classic access token (its second). The chats are the OpenAI instrumentation's; the agent, tool and
retriever spans are written with OpenInference's own tracer. The OpenAI key is a planted, fictitious one.
"""

import os

os.environ.setdefault("OPENINFERENCE_ENABLE_GENAI_SEMCONV", "true")

import json  # noqa: E402

import openai  # noqa: E402
from openinference.instrumentation import OITracer, TraceConfig, get_retriever_attributes  # noqa: E402
from openinference.instrumentation.openai import OpenAIInstrumentor  # noqa: E402
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.sdk.resources import SERVICE_NAME, Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: E402

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
    {"id": "kb-112", "score": 0.91, "content": f"Orders ship within two days. {CANARY}"},
    {"id": "kb-407", "score": 0.73, "content": f"Track an order from its page. {CANARY}"},
    {"id": "kb-019", "score": 0.52, "content": f"Return an order within 30 days. {CANARY}"},
]

scheme = os.environ.get("DT_TOKEN_SCHEME", "Bearer")
tracer_provider = TracerProvider(
    resource=Resource.create(
        {SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "support-assistant"), "service.version": "4.2.0"}
    )
)
tracer_provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(
            endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
            headers={"Authorization": f"{scheme} {os.environ['DT_API_TOKEN']}"},
        )
    )
)
OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
tracer = OITracer(tracer_provider.get_tracer(__name__), config=TraceConfig())


def get_order(order_id: str) -> dict:
    with tracer.start_as_current_span("get_order", openinference_span_kind="tool") as span:
        span.set_tool(name="get_order", description="Look up an order", parameters=TOOLS[0]["function"]["parameters"])
        span.set_input({"order_id": order_id})
        result = {"status": "shipped", "note": CANARY}
        span.set_output(result)
        return result


def search_help_centre(query: str) -> list:
    with tracer.start_as_current_span("help-centre", openinference_span_kind="retriever") as span:
        span.set_input(query)
        span.set_attributes(get_retriever_attributes(documents=DOCUMENTS))
        return DOCUMENTS


def main() -> None:
    client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "planted-openai-key"))
    user = {"role": "user", "content": f"Where is order 1042? {CANARY}"}
    with tracer.start_as_current_span(
        "support", openinference_span_kind="agent", attributes={"agent.name": "support"}
    ) as agent:
        agent.set_input(user["content"])
        first = client.chat.completions.create(model="gpt-4o", messages=[user], tools=TOOLS)
        call = first.choices[0].message.tool_calls[0]
        result = get_order(json.loads(call.function.arguments)["order_id"])
        search_help_centre(f"When does an order ship? {CANARY}")
        last = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                user,
                first.choices[0].message.model_dump(exclude_none=True),
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)},
            ],
            tools=TOOLS,
        )
        agent.set_output(last.choices[0].message.content)
    tracer_provider.shutdown()


if __name__ == "__main__":
    main()

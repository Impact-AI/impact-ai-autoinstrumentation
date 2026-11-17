#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The canonical interaction, written by one AI tool's own SDK, for recording what that SDK sends its backend.

    THEIRS_URL=http://127.0.0.1:8080 PROVIDER_URL=http://127.0.0.1:8091 python3 canonical.py traceloop

An agent `support` asks a chat model (`gpt-4o`, answered as `gpt-4o-2024-08-06` by provider.py) where an order is;
the model asks for the tool `get_order`; the tool runs; a retrieval over the help centre returns three documents of a
top-k of five; a second chat answers. Every prompt, reply, tool argument and result and the retrieval's query carry
the planted text CONTENT-CANARY. Each tool writes it with its own API, the way its documentation shows, and exports
to THEIRS_URL through the one setting the kit switches when it copies (the tool's base URL or the OTLP traces
endpoint), with the planted credentials below in the tool's own headers.
"""

from __future__ import annotations

import json
import os
import sys

CANARY = "CONTENT-CANARY"
QUESTION = f"Where is order 1042? {CANARY}"
QUERY = f"When does an order ship? {CANARY}"
RESULT = {"status": "shipped", "note": CANARY}
DOCUMENTS = [
    {"id": "kb-112", "score": 0.91, "text": f"Orders ship within two days. {CANARY}"},
    {"id": "kb-407", "score": 0.73, "text": f"Tracking numbers arrive by e-mail. {CANARY}"},
    {"id": "kb-019", "score": 0.52, "text": f"Weekend orders ship on Monday. {CANARY}"},
]
TOP_K = 5
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Look up an order by its id.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    }
]
SERVICE = {"service.name": "support-assistant", "service.version": "4.2.0"}

# Planted, fictitious credentials: each must reach the tool's backend and nothing else.
PLANTED = {
    "traceloop": "planted-traceloop-key",
    "phoenix": "planted-phoenix-key",
    "langfuse": ("pk-lf-planted", "sk-lf-planted"),
    "logfire": "pylf_v1_us_planted",
    "braintrust": "planted-braintrust-key",
    "honeycomb": "planted-honeycomb-key",
    "arize-ax": ("planted-arize-key", "planted-space"),
}


def theirs() -> str:
    return os.environ["THEIRS_URL"].rstrip("/")


def openai_client():
    from openai import OpenAI

    return OpenAI(api_key="sk-test", base_url=os.environ["PROVIDER_URL"].rstrip("/") + "/v1")


def first_turn(client):
    messages = [{"role": "user", "content": QUESTION}]
    reply = client.chat.completions.create(model="gpt-4o", messages=messages, tools=TOOLS)
    return messages, reply.choices[0].message


def second_turn(client, messages, message, result: str):
    call = message.tool_calls[0]
    messages = [
        *messages,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call.id, "content": result},
    ]
    reply = client.chat.completions.create(model="gpt-4o", messages=messages, tools=TOOLS)
    return reply.choices[0].message.content


def lookup(arguments: str) -> str:
    """The tool: what get_order returns for its arguments."""
    json.loads(arguments)
    return json.dumps(RESULT, separators=(",", ":"))


# Traceloop (OpenLLMetry): Traceloop.init() owns the provider and exports OTLP/HTTP to TRACELOOP_BASE_URL/v1/traces
# with Authorization: Bearer TRACELOOP_API_KEY, and instruments every library it finds (OpenAI, Qdrant, LangChain).
def traceloop_init() -> None:
    os.environ["TRACELOOP_BASE_URL"] = theirs()
    os.environ["TRACELOOP_API_KEY"] = PLANTED["traceloop"]
    os.environ["TRACELOOP_METRICS_ENABLED"] = "false"
    from traceloop.sdk import Traceloop

    Traceloop.init(
        app_name=SERVICE["service.name"],
        resource_attributes={"service.version": SERVICE["service.version"]},
        disable_batch=True,
        telemetry_enabled=False,
    )


# The help centre: an in-memory Qdrant collection of the three documents, which OpenLLMetry's Qdrant instrumentation
# traces as the retrieval when it is searched with search_help_centre.
def help_centre():
    from qdrant_client import QdrantClient, models

    store = QdrantClient(":memory:")
    store.create_collection("help-centre", vectors_config=models.VectorParams(size=2, distance=models.Distance.DOT))
    store.upsert(
        "help-centre",
        points=[
            models.PointStruct(id=index + 1, vector=[1.0, 1.0 - index / 10], payload=document)
            for index, document in enumerate(DOCUMENTS)
        ],
    )
    return store


def search_help_centre(store) -> None:
    store.query_points("help-centre", query=[1.0, 1.0], limit=TOP_K, with_payload=True)


# Traceloop's own API: agents and tools are its decorators; the retrieval is the help centre's Qdrant search.
def traceloop() -> None:
    traceloop_init()
    from traceloop.sdk import Traceloop
    from traceloop.sdk.decorators import agent, tool

    store = help_centre()

    @tool(name="get_order")
    def get_order(arguments: str) -> str:
        return lookup(arguments)

    @agent(name="support")
    def support() -> str:
        client = openai_client()
        messages, message = first_turn(client)
        result = get_order(message.tool_calls[0].function.arguments)
        search_help_centre(store)
        return second_turn(client, messages, message, result)

    support()
    Traceloop.get_tracer_provider().force_flush() if hasattr(Traceloop, "get_tracer_provider") else None


def chat_model():
    """gpt-4o through LangChain's OpenAI chat model, on provider.py's chat completions."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model="gpt-4o",
        api_key="sk-test",
        base_url=os.environ["PROVIDER_URL"].rstrip("/") + "/v1",
        use_responses_api=False,
    )


def get_order_tool():
    """get_order as a LangChain tool: the arguments arrive as the model wrote them, and it answers as lookup() does."""
    from langchain_core.tools import tool

    @tool("get_order", description=TOOLS[0]["function"]["description"])
    def get_order(order_id: str) -> str:
        return lookup(json.dumps({"order_id": order_id}))

    return get_order


# LangChain (OpenLLMetry's LangChain instrumentation, through Traceloop.init()): the agent is LangChain's create_agent,
# which runs on LangGraph, as agents built with LangChain 1 are (Orion's shop agent records ls_integration
# langchain_create_agent). The tool is LangChain's @tool, and the help centre is searched by a middleware step before
# the model's turn that follows the tool's answer.
def langchain() -> None:
    traceloop_init()
    from langchain.agents import create_agent
    from langchain.agents.middleware import before_model

    store = help_centre()

    @before_model
    def search_after_tool(state, runtime):
        if state["messages"] and state["messages"][-1].type == "tool":
            search_help_centre(store)

    agent = create_agent(model=chat_model(), tools=[get_order_tool()], middleware=[search_after_tool], name="support")
    agent.invoke({"messages": [{"role": "user", "content": QUESTION}]})


# LangChain as the OpenTelemetry demo's shop agent builds it (src/agent/src/agents/agents.py): create_agent with no
# name, run inside a Traceloop workflow named after the agent. LangGraph then names the agent after itself, so only the
# workflow says which agent it is.
def langchain_workflow() -> None:
    traceloop_init()
    from langchain.agents import create_agent
    from langchain.agents.middleware import before_model
    from traceloop.sdk.decorators import workflow

    store = help_centre()

    @before_model
    def search_after_tool(state, runtime):
        if state["messages"] and state["messages"][-1].type == "tool":
            search_help_centre(store)

    @workflow(name="support")
    def support() -> None:
        agent = create_agent(model=chat_model(), tools=[get_order_tool()], middleware=[search_after_tool])
        agent.invoke({"messages": [{"role": "user", "content": QUESTION}]})

    support()


# LangGraph (OpenLLMetry's LangChain instrumentation, which also traces LangGraph): the agent is a graph built with
# LangGraph's own API, as its documentation shows: the model's node, LangGraph's ToolNode, and the help centre's search
# between the tool and the model's next turn, compiled under the agent's name.
def langgraph() -> None:
    traceloop_init()
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    store = help_centre()
    get_order = get_order_tool()
    model = chat_model().bind_tools([get_order])

    def call_model(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    def search(state: MessagesState):
        search_help_centre(store)
        return {}

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode([get_order]))
    graph.add_node("help_centre", search)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "help_centre")
    graph.add_edge("help_centre", "model")
    graph.compile(name="support").invoke({"messages": [{"role": "user", "content": QUESTION}]})


# Arize Phoenix: phoenix.otel.register() owns the provider and exports to PHOENIX_COLLECTOR_ENDPOINT with
# Authorization: Bearer PHOENIX_API_KEY; OpenInference's OpenAI instrumentor traces the model calls. Over HTTP the
# endpoint gets /v1/traces; a bare origin with protocol inferred would mean gRPC to PHOENIX_GRPC_PORT instead. Agents,
# tools and retrievers are the OpenInference tracer's own spans, the retrieved documents its retriever attributes.
def phoenix() -> None:
    os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = theirs()
    os.environ["PHOENIX_API_KEY"] = PLANTED["phoenix"]
    from openinference.instrumentation import get_retriever_attributes
    from openinference.semconv.trace import SpanAttributes
    from phoenix.otel import register

    provider = register(
        project_name=SERVICE["service.name"],
        protocol="http/protobuf",
        batch=False,
        auto_instrument=True,
        set_global_tracer_provider=True,
        verbose=False,
    )
    tracer = provider.get_tracer(__name__)

    @tracer.tool(name="get_order")
    def get_order(arguments: str) -> str:
        return lookup(arguments)

    def search(query: str) -> list[dict]:
        with tracer.start_as_current_span("help-centre", openinference_span_kind="retriever") as span:
            span.set_input(query)
            documents = [{"id": d["id"], "score": d["score"], "content": d["text"]} for d in DOCUMENTS]
            span.set_attributes(get_retriever_attributes(documents=documents))
            return documents

    @tracer.agent(name="support")
    def support() -> str:
        from opentelemetry import trace

        trace.get_current_span().set_attribute(SpanAttributes.AGENT_NAME, "support")
        client = openai_client()
        messages, message = first_turn(client)
        result = get_order(message.tool_calls[0].function.arguments)
        search(QUERY)
        return second_turn(client, messages, message, result)

    support()


# Langfuse (v3 and later SDKs, built on OpenTelemetry): the client owns a span processor that exports OTLP/HTTP to
# LANGFUSE_BASE_URL/api/public/otel/v1/traces with Basic auth (public and secret key) and its x-langfuse-* headers.
# Agents, tools and retrievers are observations of those types; the model calls go through its OpenAI drop-in.
def langfuse() -> None:
    os.environ["LANGFUSE_BASE_URL"] = theirs()
    os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"] = PLANTED["langfuse"]
    os.environ.setdefault("LANGFUSE_RELEASE", SERVICE["service.version"])
    from langfuse import get_client, observe
    from langfuse.openai import OpenAI

    client = get_client()

    @observe(as_type="tool", name="get_order")
    def get_order(arguments: str) -> str:
        return lookup(arguments)

    def search(query: str) -> list[dict]:
        with client.start_as_current_observation(as_type="retriever", name="help-centre", input=query) as span:
            span.update(output=DOCUMENTS)
            return DOCUMENTS

    @observe(as_type="agent", name="support")
    def support() -> str:
        openai = OpenAI(api_key="sk-test", base_url=os.environ["PROVIDER_URL"].rstrip("/") + "/v1")
        messages, message = first_turn(openai)
        result = get_order(message.tool_calls[0].function.arguments)
        search(QUERY)
        return second_turn(openai, messages, message, result)

    support()
    client.flush()


# Pydantic Logfire: logfire.configure() owns the provider and exports OTLP/HTTP to LOGFIRE_BASE_URL/v1/traces (the
# token's region otherwise) with Authorization: <write token>. An agent is traced the way Logfire traces one: a Pydantic
# AI agent with logfire.instrument_pydantic_ai(), which writes the agent run, its chats and its tool call. Pydantic AI
# has no retrieval step, so the retrieval is a Logfire span in the OpenTelemetry GenAI terms Logfire's own
# integrations use, its message template naming the data source and not the query.
def logfire() -> None:
    os.environ["LOGFIRE_BASE_URL"] = theirs()
    os.environ["LOGFIRE_TOKEN"] = PLANTED["logfire"]
    import logfire as sdk
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    sdk.configure(
        service_name=SERVICE["service.name"],
        service_version=SERVICE["service.version"],
        send_to_logfire=True,
        console=False,
        inspect_arguments=False,
        metrics=False,
    )
    sdk.instrument_pydantic_ai()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key="sk-test", base_url=os.environ["PROVIDER_URL"].rstrip("/") + "/v1")
    model = OpenAIChatModel("gpt-4o", provider=OpenAIProvider(openai_client=client))
    agent = Agent(model, name="support")

    @agent.tool_plain
    def get_order(order_id: str) -> str:
        """Look up an order by its id."""
        reply = lookup(json.dumps({"order_id": order_id}))
        search(QUERY)
        # A batch exported while the agent run is still open, as in any longer run: Logfire sends pending spans for it.
        sdk.force_flush()
        return reply

    def search(query: str) -> list[dict]:
        attributes = {
            "gen_ai.operation.name": "retrieval",
            "gen_ai.data_source.id": "help-centre",
            "gen_ai.request.top_k": TOP_K,
            "gen_ai.retrieval.query.text": query,
            "gen_ai.retrieval.documents": json.dumps([{"id": d["id"], "score": d["score"]} for d in DOCUMENTS]),
        }
        with sdk.span("retrieval {gen_ai.data_source.id}", **attributes):
            return DOCUMENTS

    agent.run_sync(QUESTION)
    sdk.force_flush()


def genai_interaction(client) -> None:
    """The interaction with the agent, the tool and the retrieval as spans in the OpenTelemetry GenAI conventions, the
    way an app writes them beside an instrumentation that traces only the model calls."""
    from opentelemetry import trace

    tracer = trace.get_tracer("support-assistant")
    agent = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "support"}
    with tracer.start_as_current_span("invoke_agent support", attributes=agent):
        messages, message = first_turn(client)
        call = message.tool_calls[0]
        tool = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": call.function.name,
            "gen_ai.tool.call.id": call.id,
            "gen_ai.tool.call.arguments": call.function.arguments,
        }
        with tracer.start_as_current_span(f"execute_tool {call.function.name}", attributes=tool) as span:
            result = lookup(call.function.arguments)
            span.set_attribute("gen_ai.tool.call.result", result)
        retrieval = {
            "gen_ai.operation.name": "retrieval",
            "gen_ai.data_source.id": "help-centre",
            "gen_ai.request.top_k": TOP_K,
            "gen_ai.retrieval.query.text": QUERY,
            "gen_ai.retrieval.documents": json.dumps([{"id": d["id"], "score": d["score"]} for d in DOCUMENTS]),
        }
        with tracer.start_as_current_span("retrieval help-centre", attributes=retrieval):
            pass
        second_turn(client, messages, message, result)


def genai_content() -> None:
    """OpenTelemetry's GenAI instrumentations keep content on the span only when asked to."""
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_ONLY")


def otlp_sdk(headers: str = "") -> None:
    """The OpenTelemetry SDK an app configures itself: an OTLP/HTTP exporter set by the standard variables, and
    OpenTelemetry's OpenAI instrumentation."""
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = theirs()
    os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
    if headers:
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = headers
    genai_content()
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create(SERVICE))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    OpenAIInstrumentor().instrument()
    genai_interaction(openai_client())


# Honeycomb: OTLP to api.honeycomb.io through the standard endpoint variable, with the x-honeycomb-team header.
def honeycomb() -> None:
    otlp_sdk(f"x-honeycomb-team={PLANTED['honeycomb']}")


# Grafana Alloy: the app sends OTLP to the Alloy collector in its cluster, which needs no header of the app's.
def grafana_alloy() -> None:
    otlp_sdk()


# Arize AX: arize.otel.register() owns the provider and exports to ARIZE_COLLECTOR_ENDPOINT (gRPC to
# otlp.arize.com/v1 by default; HTTP here, against the recording mock) with authorization and arize-space-id headers;
# OpenInference traces the model calls and writes the agent, tool and retriever spans, as with Phoenix.
def arize_ax() -> None:
    # Over gRPC the endpoint is an address (https://otlp.arize.com/v1); over HTTP the exporter posts to it as given.
    os.environ["ARIZE_COLLECTOR_ENDPOINT"] = theirs() + "/v1/traces"
    os.environ["ARIZE_API_KEY"], os.environ["ARIZE_SPACE_ID"] = PLANTED["arize-ax"]
    from arize.otel import Transport, register
    from openinference.instrumentation import get_retriever_attributes
    from openinference.instrumentation.openai import OpenAIInstrumentor
    from openinference.semconv.trace import SpanAttributes

    # verbose=False: printing the exporter's headers fails on the HTTP exporter of OpenTelemetry 1.45.
    provider = register(project_name=SERVICE["service.name"], transport=Transport.HTTP, batch=False, verbose=False)
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    from openinference.instrumentation import OITracer, TraceConfig

    tracer = OITracer(provider.get_tracer(__name__), config=TraceConfig())

    @tracer.tool(name="get_order")
    def get_order(arguments: str) -> str:
        return lookup(arguments)

    @tracer.agent(name="support")
    def support() -> str:
        from opentelemetry import trace

        trace.get_current_span().set_attribute(SpanAttributes.AGENT_NAME, "support")
        client = openai_client()
        messages, message = first_turn(client)
        result = get_order(message.tool_calls[0].function.arguments)
        with tracer.start_as_current_span("help-centre", openinference_span_kind="retriever") as span:
            span.set_input(QUERY)
            documents = [{"id": d["id"], "score": d["score"], "content": d["text"]} for d in DOCUMENTS]
            span.set_attributes(get_retriever_attributes(documents=documents))
        return second_turn(client, messages, message, result)

    support()


# Braintrust's OTLP ingestion: BraintrustSpanProcessor exports to BRAINTRUST_API_URL/otel/v1/traces with
# Authorization: Bearer <key> and x-bt-parent; the app's own instrumentation says what the spans are.
def braintrust() -> None:
    os.environ["BRAINTRUST_API_URL"] = theirs()
    os.environ["BRAINTRUST_API_KEY"] = PLANTED["braintrust"]
    os.environ["BRAINTRUST_PARENT"] = f"project_name:{SERVICE['service.name']}"
    genai_content()
    from braintrust.otel import BraintrustSpanProcessor
    from opentelemetry import trace
    from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create(SERVICE))
    provider.add_span_processor(BraintrustSpanProcessor())
    trace.set_tracer_provider(provider)
    OpenAIInstrumentor().instrument()
    genai_interaction(openai_client())


# A vendor distribution started by its launcher (opentelemetry-instrument), which configures the SDK, the exporter
# and the instrumentations from the environment record.py passes: the app only writes its own spans.
def distribution() -> None:
    genai_interaction(openai_client())


RUNS = {
    "traceloop": traceloop,
    "langchain": langchain,
    "langchain-workflow": langchain_workflow,
    "langgraph": langgraph,
    "phoenix": phoenix,
    "langfuse": langfuse,
    "logfire": logfire,
    "honeycomb": honeycomb,
    "grafana-alloy": grafana_alloy,
    "arize-ax": arize_ax,
    "braintrust": braintrust,
    "elastic-edot": distribution,
    "splunk-otel": distribution,
}


def main() -> int:
    tool = sys.argv[1] if len(sys.argv) > 1 else ""
    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE["service.name"])
    os.environ.setdefault("OTEL_RESOURCE_ATTRIBUTES", f"service.version={SERVICE['service.version']}")
    if tool not in RUNS:
        print(f"usage: canonical.py {{{','.join(RUNS)}}}", file=sys.stderr)
        return 2
    RUNS[tool]()
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

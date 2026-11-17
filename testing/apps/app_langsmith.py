# SPDX-License-Identifier: Apache-2.0

"""Fixture workload (ASGI): an application that already traces with LangSmith, the way LangSmith's own guides set it up.

LangSmith's default tracing is on (LANGSMITH_TRACING=true) and sends to LANGSMITH_ENDPOINT, which the tests point at
the recording mock's `theirs` instance. Every route runs one kind of LangSmith tracing:

  /langsmith/langchain        a LangChain v1 agent (`create_agent`): LangChain's callbacks
  /langsmith/langgraph        LangGraph's prebuilt ReAct agent: LangGraph's callbacks
  /langsmith/langgraph-graph  a graph of the application's own: a workflow, not an agent
  /langsmith/traceable        a function marked `@traceable` that calls OpenAI through LangSmith's `wrap_openai`
  /langsmith/support          the support assistant of the span-source fixtures: an agent `support` whose model asks for
                              the tool `get_order`, which reads a help centre (a retriever of top-k 5 that returns three
                              documents), and then answers; 120 + 300 input and 20 + 80 output tokens, every piece of
                              content carrying CONTENT-CANARY. Its model is answered by a provider inside this process,
                              because the mock's fixed answers have other tokens.

POST /flush waits until LangSmith has sent everything it holds (its background thread and, in its OpenTelemetry mode,
the tracer provider it set), which is what the tests call before they read `theirs`. The app has no tracing of its own
beyond LangSmith's, and imports each framework on first use, like the other fixtures.
"""

import json
import os
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
STUB = os.environ["STUB_URL"]
CANARY = "CONTENT-CANARY"
PROMPT = "What is the weather in Paris?"


class Ask(BaseModel):
    prompt: str = PROMPT


@app.get("/healthz")
def healthz():
    return {"ok": True}


def get_weather(city: str) -> str:
    """Weather of a city."""
    return f"Sunny in {city}"


def chat_model(base_url: str = STUB + "/v1", model: str = "gpt-4o-mini"):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model, base_url=base_url, api_key="sk-test")


@app.post("/langsmith/langchain")
def langchain_agent(ask: Ask):
    """A LangChain v1 agent, which runs on LangGraph."""
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    agent = create_agent(chat_model(), tools=[tool(get_weather)], name="weather_agent", system_prompt="be brief")
    out = agent.invoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}


@app.post("/langsmith/langgraph")
def langgraph_agent(ask: Ask):
    """LangGraph's prebuilt ReAct agent."""
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # deprecated in favour of langchain's create_agent, still what many apps run
        agent = create_react_agent(chat_model(), tools=[tool(get_weather)], name="react_agent")
    out = agent.invoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}


@app.post("/langsmith/langgraph-graph")
def langgraph_graph(ask: Ask):
    """A graph of the application's own: model node, tool node and a loop."""
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    tools = [tool(get_weather)]
    model = chat_model().bind_tools(tools)

    def call_model(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    def route(state: MessagesState):
        return "tools" if state["messages"][-1].tool_calls else END

    graph = StateGraph(MessagesState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, ["tools", END])
    graph.add_edge("tools", "model")
    out = graph.compile(name="weather_graph").invoke({"messages": [{"role": "user", "content": ask.prompt}]})
    return {"answer": out["messages"][-1].content}


@app.post("/langsmith/traceable")
def traceable_function(ask: Ask):
    """LangSmith's own tracing without LangChain: a traceable function and the OpenAI client wrapper."""
    from langsmith import traceable
    from langsmith.wrappers import wrap_openai
    from openai import OpenAI

    client = wrap_openai(OpenAI(base_url=STUB + "/v1", api_key="sk-test"))

    @traceable(run_type="tool", name="get_weather")
    def weather(city: str) -> str:
        return get_weather(city)

    @traceable(name="plan_trip")
    def plan(question: str) -> str:
        answer = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": question}])
        return f"{answer.choices[0].message.content} {weather('Paris')}"

    return {"answer": plan(ask.prompt)}


# The support assistant's model, answered here: the first call asks for get_order, the call with its result answers.
SUPPORT_MODEL = "gpt-4o-2024-08-06"


class _SupportProvider(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        answered = any(message.get("role") == "tool" for message in request.get("messages") or [])
        if answered:
            message = {"role": "assistant", "content": f"Order 1042 has shipped. {CANARY}"}
            finish, usage = "stop", (300, 80)
        else:
            arguments = json.dumps({"order_id": f"1042 {CANARY}"})
            call = {"id": "call_1", "type": "function", "function": {"name": "get_order", "arguments": arguments}}
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish, usage = "tool_calls", (120, 20)
        body = json.dumps(
            {
                "id": "chatcmpl-support",
                "object": "chat.completion",
                "created": 1,
                "model": SUPPORT_MODEL,
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


_support_url = None
_support_lock = threading.Lock()


def support_provider() -> str:
    global _support_url
    with _support_lock:
        if _support_url is None:
            port = int(os.environ.get("SUPPORT_PROVIDER_PORT", "8765"))  # fixed: LangSmith records the model's base URL
            server = ThreadingHTTPServer(("127.0.0.1", port), _SupportProvider)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            _support_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    return _support_url


@app.post("/langsmith/support")
def support(ask: Ask):
    """The support assistant every span source reproduces: one agent, one tool, one retrieval, two model calls."""
    from langchain.agents import create_agent
    from langchain_core.callbacks import CallbackManagerForRetrieverRun
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever
    from langchain_core.tools import tool

    class HelpCentre(BaseRetriever):
        """The help centre: three articles of the five it was asked for."""

        k: int = 5

        def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun):
            found = [("kb-112", 0.91), ("kb-407", 0.73), ("kb-019", 0.52)]
            return [
                Document(id=key, page_content=f"Article {key} {CANARY}", metadata={"id": key, "score": score})
                for key, score in found[: self.k]
            ]

    @tool
    def get_order(order_id: str) -> str:
        """Look up an order and how its shipping works."""
        HelpCentre().invoke(f"When does an order ship? {CANARY}")
        return json.dumps({"status": "shipped", "note": CANARY})

    agent = create_agent(chat_model(support_provider(), SUPPORT_MODEL), tools=[get_order], name="support")
    question = f"Where is order 1042? {CANARY}" if ask.prompt == PROMPT else ask.prompt
    out = agent.invoke({"messages": [{"role": "user", "content": question}]})
    return {"answer": out["messages"][-1].content}


@app.post("/flush")
def flush():
    """Send everything LangSmith holds now: its runs and, in its OpenTelemetry mode, its spans."""
    from langchain_core.tracers.langchain import wait_for_all_tracers
    from langsmith.run_trees import get_cached_client

    wait_for_all_tracers()
    get_cached_client().flush()
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
    except ImportError:
        pass
    return {"ok": True}

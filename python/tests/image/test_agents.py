# SPDX-License-Identifier: Apache-2.0

"""Agents are first-class. The OpenAI Agents SDK, LangChain and LangGraph are covered by the payload alone.

The qualification with mock models (OpenAI chat, and the Responses API the Agents SDK calls). For each framework an
agent with one tool runs one round of its loop in a real application image, injected the way the Operator injects it,
and the spans that reach the gateway must show:

* the agent step: `gen_ai.operation.name` invoke_agent, with `gen_ai.agent.name`;
* the tool step: execute_tool, with the tool's name, arguments and result;
* the model calls: one chat span per call (two here), with prompt and answer;
* all of it in one trace, under the request's server span, with the workload's placement;
* nothing of the content when the workload asks for none.

What is proven is what is in this file and nothing else. LlamaIndex, CrewAI and MCP are not: the setup report lists them
as untraced (test_the_setup_report_lists_the_frameworks_and_those_the_image_cannot_trace).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from test_injected_apps import wait_ready

from conftest import app_image, require_images
from containers import http, wait_until

# The agent images are the largest: CI builds them for these Pythons (3.14 is the one an agent image of ours runs),
# `AGENTS_PYTHONS="3.11 3.12 3.13 3.14"` for all four.
PYTHONS = os.environ.get("AGENTS_PYTHONS", "3.12 3.13 3.14").split()
pytestmark = [pytest.mark.image, pytest.mark.agents]
UVICORN = ["uvicorn", "app_agents:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
PROMPT, ANSWER, TOOL_RESULT = "What is the weather in Paris?", "Hello from the stub.", "Sunny in Paris"
PINS = Path(__file__).resolve().parents[3] / "testing" / "apps" / "requirements-agents.txt"

# endpoint -> (the agent's name, the model API, the root operation of the trace)
SCENARIOS = {
    "openai-agents": ("Weather agent", "responses", "invoke_workflow"),
    "openai-agents-chat": ("Weather agent", "chat", "invoke_workflow"),
    "langchain": ("weather_agent", "chat", "invoke_agent"),
    "langchain-async": ("weather_agent", "chat", "invoke_agent"),
    "langgraph": ("react_agent", "chat", "invoke_agent"),
}
CONTENT_ATTRIBUTES = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
)


def operation(span: dict) -> str | None:
    return span["attrs"].get("gen_ai.operation.name")


def run_agent(app, scenario: str) -> dict:
    status, body = http("POST", f"{app.url}/agent/{scenario}", {"prompt": PROMPT}, timeout=90)
    assert status == 200, (scenario, body, app.logs())
    assert body == {"answer": ANSWER}  # the application behaves exactly as without injection
    return body


def trace_of(mock, agent_name: str) -> list[dict]:
    """The spans of the trace that holds the agent step, once the request's server span has been exported too."""
    spans = mock.spans()
    agents = [s for s in spans if operation(s) == "invoke_agent" and s["attrs"].get("gen_ai.agent.name") == agent_name]
    if not agents:
        return []
    trace = [s for s in spans if s["traceId"] == agents[0]["traceId"]]
    return trace if any(s.get("kind") == 2 for s in trace) and sum(operation(s) == "chat" for s in trace) >= 2 else []


def descends_from(span: dict, ancestor: dict, by_id: dict) -> bool:
    seen = set()
    while span.get("parentSpanId") and span["parentSpanId"] not in seen:
        seen.add(span["parentSpanId"])
        if span["parentSpanId"] == ancestor["spanId"]:
            return True
        span = by_id.get(span["parentSpanId"], {})
    return False


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("python", PYTHONS)
def test_the_agent_its_tool_and_its_model_calls_arrive_in_one_trace(start_app, mock, python, scenario):
    require_images(app_image(python, "agents"))
    agent_name, api, root_operation = SCENARIOS[scenario]
    app = start_app(app_image(python, "agents"), UVICORN)
    wait_ready(app)
    run_agent(app, scenario)

    trace = wait_until(lambda: trace_of(mock, agent_name), timeout=60, message=lambda: f"the trace\n{app.logs()}")
    by_id = {s["spanId"]: s for s in trace}
    (agent,) = [s for s in trace if operation(s) == "invoke_agent"]
    (server,) = [s for s in trace if s.get("kind") == 2]
    tools = [s for s in trace if operation(s) == "execute_tool"]
    chats = [s for s in trace if operation(s) == "chat"]

    # The agent step.
    assert agent["attrs"]["gen_ai.agent.name"] == agent_name
    assert descends_from(agent, server, by_id)
    assert server["name"] == f"POST /agent/{scenario}"

    # The tool step: its name, what the model asked for, what the tool answered.
    (tool,) = tools
    assert tool["attrs"]["gen_ai.tool.name"] == "get_weather"
    assert tool["attrs"]["gen_ai.tool.type"] == "function"
    assert "Paris" in tool["attrs"]["gen_ai.tool.call.arguments"]
    assert tool["attrs"]["gen_ai.tool.call.result"] == TOOL_RESULT
    assert descends_from(tool, agent, by_id)

    # The model calls: one chat span per call, so tokens are counted once.
    assert len(chats) == 2, [(s["name"], s["spanId"], s.get("parentSpanId")) for s in trace]
    first, second = sorted(chats, key=lambda s: int(s["startTimeUnixNano"]))
    for chat in chats:
        assert chat["attrs"]["gen_ai.provider.name"] == "openai"
        assert chat["attrs"]["gen_ai.request.model"] == "gpt-4o-mini"
        assert chat["attrs"]["gen_ai.usage.input_tokens"] == 7
        assert chat["attrs"]["gen_ai.usage.output_tokens"] == 5
        assert PROMPT in chat["attrs"]["gen_ai.input.messages"]
        assert descends_from(chat, agent, by_id)
        assert (chat["attrs"].get("openai.api.type") == "responses") == (api == "responses"), chat["attrs"]
    assert "get_weather" in first["attrs"]["gen_ai.output.messages"]  # the model asks for the tool
    assert ANSWER in second["attrs"]["gen_ai.output.messages"]  # and answers once it has the result

    # What the trace is rooted in, and the workload's placement on every span.
    assert any(operation(s) == root_operation for s in trace)
    for span in trace:
        assert span["resource"]["k8s.namespace.name"] == "shop"
        assert span["resource"]["k8s.deployment.name"] == "support-assistant"
        assert span["resource"]["service.name"] == "support-assistant"


@pytest.mark.parametrize("python", PYTHONS)
def test_a_langchain_agent_an_unnamed_agent_with_an_async_tool_is_traced_and_named(start_app, mock, python):
    # The shape of an agent service: create_agent without a name, a ChatOpenAI subclass with its own async HTTP
    # client, an async tool, ainvoke in an async route. The instrumentation has no name for such an agent; the package
    # names it after the workload so that every agent span says which agent it is.
    require_images(app_image(python, "agents"))
    app = start_app(app_image(python, "agents"), UVICORN)
    wait_ready(app)
    status, body = http("POST", f"{app.url}/agent/langchain-unnamed", {"prompt": PROMPT}, timeout=90)
    assert (status, body) == (200, {"answer": ANSWER}), app.logs()

    trace = wait_until(
        lambda: trace_of(mock, "support-assistant"), timeout=60, message=lambda: f"the trace\n{app.logs()}"
    )
    by_id = {s["spanId"]: s for s in trace}
    (agent,) = [s for s in trace if operation(s) == "invoke_agent"]
    (server,) = [s for s in trace if s.get("kind") == 2]
    (tool,) = [s for s in trace if operation(s) == "execute_tool"]
    chats = [s for s in trace if operation(s) == "chat"]
    assert agent["name"] == "invoke_agent support-assistant"
    assert agent["attrs"]["gen_ai.agent.name"] == "support-assistant"  # the workload's name: the agent had none
    assert descends_from(agent, server, by_id) and descends_from(tool, agent, by_id)
    assert tool["attrs"]["gen_ai.tool.name"] == "get_weather_async"
    assert tool["attrs"]["gen_ai.tool.call.result"] == TOOL_RESULT
    assert len(chats) == 2 and all(descends_from(chat, agent, by_id) for chat in chats)
    assert PROMPT in agent["attrs"]["gen_ai.input.messages"] and ANSWER in agent["attrs"]["gen_ai.output.messages"]


def test_langgraph_without_the_langchain_package_is_traced_through_langchain_core(start_app, mock):
    # An image with langgraph, langchain-core and langchain-openai but no `langchain`: the LangChain instrumentation is
    # for langchain, and arms through langchain-core (runtime.ALTERNATIVES).
    require_images(app_image("3.12", "langgraph"))
    app = start_app(app_image("3.12", "langgraph"), UVICORN)
    wait_ready(app)
    run_agent(app, "langgraph")

    trace = wait_until(lambda: trace_of(mock, "react_agent"), timeout=60, message=lambda: f"the trace\n{app.logs()}")
    assert {operation(s) for s in trace} >= {"invoke_agent", "execute_tool", "chat"}
    assert len([s for s in trace if operation(s) == "chat"]) == 2
    setup = next(s for s in mock.spans() if s["name"] == "impact.setup")
    names = [entry.split()[0] for entry in setup["attrs"]["impact.setup.frameworks"]]
    assert names == ["langgraph", "anthropic", "openai"]  # no langchain, and langchain-core is not listed on its own
    assert setup["attrs"]["impact.setup.untraced"] == []


def test_a_graph_of_the_applications_own_is_a_workflow_with_its_tool_and_model_spans(start_app, mock):
    # LangGraph without an agent: the application wired the model node, the tool node and the loop itself. The spans
    # say workflow, not agent, and the tool and model steps are there.
    require_images(app_image("3.12", "agents"))
    app = start_app(app_image("3.12", "agents"), UVICORN)
    wait_ready(app)
    run_agent(app, "langgraph-graph")

    def spans():
        found = [s for s in mock.spans() if operation(s) == "invoke_workflow"]
        if not found:
            return []
        trace = [s for s in mock.spans() if s["traceId"] == found[0]["traceId"]]
        return (
            trace if any(s.get("kind") == 2 for s in trace) and sum(operation(s) == "chat" for s in trace) >= 2 else []
        )

    trace = wait_until(spans, timeout=60, message=lambda: f"the trace\n{app.logs()}")
    workflow = next(s for s in trace if operation(s) == "invoke_workflow")
    assert workflow["attrs"]["gen_ai.workflow.name"] == "weather_graph"
    assert not [s for s in trace if operation(s) == "invoke_agent"]
    assert [s["attrs"]["gen_ai.tool.name"] for s in trace if operation(s) == "execute_tool"] == ["get_weather"]
    assert sum(operation(s) == "chat" for s in trace) == 2


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_content_reaches_the_spans_when_the_workload_asks_for_none(start_app, mock, scenario):
    require_images(app_image("3.12", "agents"))
    agent_name = SCENARIOS[scenario][0]
    app = start_app(
        app_image("3.12", "agents"), UVICORN, env={"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT"}
    )
    wait_ready(app)
    run_agent(app, scenario)

    trace = wait_until(lambda: trace_of(mock, agent_name), timeout=60, message=lambda: f"the trace\n{app.logs()}")
    assert {operation(s) for s in trace} >= {"invoke_agent", "execute_tool", "chat"}
    for span in trace:
        text = json.dumps(span["attrs"])
        for secret in (PROMPT, ANSWER, TOOL_RESULT, "be brief"):
            assert secret not in text, (span["name"], secret)
        assert not set(CONTENT_ATTRIBUTES) & set(span["attrs"]), (span["name"], sorted(span["attrs"]))
    assert all(s["attrs"]["gen_ai.usage.input_tokens"] == 7 for s in trace if operation(s) == "chat")


def test_the_agents_sdks_own_tracing_keeps_working_beside_ours(start_app, mock):
    # The SDK exports its traces to OpenAI's backend (here the stub) by default. Our processor is added to its list,
    # never put in its place: the customer's dashboard on platform.openai.com keeps what it had.
    require_images(app_image("3.12", "agents"))
    app = start_app(app_image("3.12", "agents"), UVICORN)
    wait_ready(app)
    run_agent(app, "openai-agents")
    wait_until(
        lambda: mock.requests("/v1/traces/ingest"),
        timeout=40,
        message=lambda: f"the SDK's own export (its batch goes out every five seconds)\n{app.logs()}",
    )
    wait_until(lambda: trace_of(mock, "Weather agent"), timeout=30, message="our spans")


def test_the_agents_sdk_with_its_tracing_off_gives_no_agent_spans_and_the_report_says_so(start_app, mock):
    # OPENAI_AGENTS_DISABLE_TRACING: the SDK creates no trace objects, so there is nothing to turn into spans. The model
    # call is still traced (the OpenAI instrumentation), and the setup report lists the SDK as untraced.
    require_images(app_image("3.12", "agents"))
    app = start_app(app_image("3.12", "agents"), UVICORN, env={"OPENAI_AGENTS_DISABLE_TRACING": "1"})
    wait_ready(app)
    run_agent(app, "openai-agents")
    wait_until(lambda: [s for s in mock.spans() if operation(s) == "chat"], timeout=60, message=lambda: app.logs())
    names = {operation(s) for s in mock.spans()}
    assert "invoke_agent" not in names and "execute_tool" not in names
    setup = next(s for s in mock.spans() if s["name"] == "impact.setup")
    assert any(entry.startswith("openai-agents ") for entry in setup["attrs"]["impact.setup.untraced"])


def pinned(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}==([^\s;]+)", PINS.read_text(), re.MULTILINE)
    assert match, name
    return f"{name} {match[1]}"


def test_the_setup_report_lists_the_frameworks_and_those_the_image_cannot_trace(start_app, mock):
    # crewai and llama-index-core are bare metadata in this image (see the Dockerfile); mcp is the real package.
    require_images(app_image("3.12", "agents"))
    app = start_app(app_image("3.12", "agents"), UVICORN)
    wait_ready(app)
    setup = wait_until(
        lambda: next((s for s in mock.spans() if s["name"] == "impact.setup"), None),
        timeout=30,
        message=lambda: f"the setup report\n{app.logs()}",
    )
    attrs = setup["attrs"]
    assert attrs["impact.setup.state"] == "active"
    assert attrs["impact.setup.frameworks"] == [  # agent frameworks first, then provider SDKs
        "crewai 1.15.35",
        pinned("langchain"),
        pinned("langgraph"),
        "llama-index-core 0.14.25",
        pinned("mcp"),
        pinned("openai-agents"),
        pinned("anthropic"),
        pinned("openai"),
    ]
    assert attrs["impact.setup.untraced"] == ["crewai 1.15.35", "llama-index-core 0.14.25", pinned("mcp")]
    assert {"openai", "anthropic", "openai_agents", "langchain"} <= set(
        attrs["impact.setup.instrumentations"].split(",")
    )


@pytest.mark.parametrize("scenario", ["openai-agents", "langchain"])
def test_the_agents_run_the_same_without_the_payload(start_app, mock, scenario):
    require_images(app_image("3.12", "agents"))
    plain = start_app(app_image("3.12", "agents"), UVICORN, inject=False)
    wait_ready(plain)
    run_agent(plain, scenario)
    assert [s for s in mock.spans()] == []

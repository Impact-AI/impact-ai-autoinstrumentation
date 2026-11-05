# SPDX-License-Identifier: Apache-2.0

"""An agent the application left unnamed is named after its workload.

LangChain's `create_agent(model, tools=...)` without `name=` gives the instrumentation nothing to call the agent, and
its span is a bare `invoke_agent` with no `gen_ai.agent.name`. Cover your AI collects the agent names of a workload, so
the package names such an agent after the workload (the service name); a name the application gave is never touched.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from impact_instrumentation import agent_names

AGENT_NAME = "gen_ai.agent.name"
OPERATION = "gen_ai.operation.name"


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "agent"}))
    provider.add_span_processor(agent_names.AgentNames())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


def finished(exporter) -> dict:
    return {span.name: span for span in exporter.get_finished_spans()}


def test_an_unnamed_agent_is_named_after_the_workload(traced):
    tracer, exporter = traced
    with tracer.start_as_current_span("invoke_agent", attributes={OPERATION: "invoke_agent"}):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.attributes[AGENT_NAME] == "agent"
    assert span.name == "invoke_agent agent"  # the semconv name: operation and agent


def test_an_agent_the_application_named_keeps_its_name(traced):
    tracer, exporter = traced
    with tracer.start_as_current_span("invoke_agent weather_agent", attributes={AGENT_NAME: "weather_agent"}):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.attributes[AGENT_NAME] == "weather_agent"
    assert span.name == "invoke_agent weather_agent"


def test_a_name_the_instrumentation_sets_later_wins(traced):
    # util-genai may only learn the name when the agent finishes; what it sets replaces the default.
    tracer, exporter = traced
    with tracer.start_as_current_span("invoke_agent") as span:
        span.set_attribute(AGENT_NAME, "late_agent")
    assert exporter.get_finished_spans()[0].attributes[AGENT_NAME] == "late_agent"


@pytest.mark.parametrize(
    "name",
    [
        "chat gpt-4o-mini",
        "execute_tool get_weather",
        "invoke_workflow weather_graph",
        "invoke_agent_batch",
        "POST /agent",
    ],
)
def test_no_other_span_is_touched(traced, name):
    tracer, exporter = traced
    with tracer.start_as_current_span(name):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.name == name
    assert AGENT_NAME not in span.attributes


def test_the_sdks_default_service_name_is_used_when_the_workload_has_none():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({}))  # the SDK's default service name is "unknown_service..."
    provider.add_span_processor(agent_names.AgentNames())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("invoke_agent"):
        pass
    (span,) = exporter.get_finished_spans()
    assert span.attributes[AGENT_NAME] == provider.resource.attributes["service.name"]


def test_the_processor_is_part_of_the_private_provider(monkeypatch):
    from impact_instrumentation import runtime

    hooks = runtime.Hooks({"OTEL_SERVICE_NAME": "returns"}, lambda exc: None, lambda armed, skipped: None)
    provider = hooks._provider()
    try:
        processors = provider._active_span_processor._span_processors
        assert any(isinstance(p, agent_names.AgentNames) for p in processors)
    finally:
        provider.shutdown()

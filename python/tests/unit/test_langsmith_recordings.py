# SPDX-License-Identifier: Apache-2.0

"""The recordings of what LangSmith sends (testing/fixtures/sources/langsmith), read the way the proof tests read it.

The container tests (python/tests/image/test_langsmith.py, nodejs/tests/image/test_nodejs_langsmith.py) compare what
LangSmith's backend received with and without impact.ai's package through tests/langsmith_runs.py. Here that reader is
held to the recorded support assistant: every run and span is found, and only what is generated is masked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import langsmith_runs as ls

RECORDINGS = Path(__file__).resolve().parents[3] / "testing" / "fixtures" / "sources" / "langsmith"
SUPPORT = [
    ("chain", "support"),
    ("chain", "model"),
    ("llm", "ChatOpenAI"),
    ("chain", "tools"),
    ("tool", "get_order"),
    ("retriever", "HelpCentre"),
    ("chain", "model"),
    ("llm", "ChatOpenAI"),
]


def recording(name: str) -> dict:
    return json.loads((RECORDINGS / f"{name}.recording.json").read_text())


def requests(name: str) -> list[dict]:
    """A recording's requests as the mock's GET /_requests gives them."""
    return [{**request, "body_b64": request["bodyBase64"]} for request in recording(name)["requests"]]


def test_each_recording_says_how_with_what_and_when():
    for name in ("support-default", "support-otel"):
        made = recording(name)
        assert made["packages"]["langsmith"] == "0.14.17"
        assert "app_langsmith.py" in made["how"] and made["recorded"] == "2026-11-09"


def test_the_default_tracing_sends_every_run_of_the_support_assistant():
    runs = ls.masked_runs(requests("support-default"))
    assert [(run["post"]["run_type"], run["post"]["name"]) for run in runs] == SUPPORT
    # The agent's own run carries LangChain's agent mark; its nodes inherit it and say which node they are.
    agent = runs[0]["post.extra"]["metadata"]
    assert agent["ls_integration"] == "langchain_create_agent" and agent["lc_agent_name"] == "support"
    assert "langgraph_node" not in agent
    assert runs[1]["post.extra"]["metadata"]["langgraph_node"] == "model"


def test_masking_replaces_what_is_generated_and_nothing_else():
    runs = ls.masked_runs(requests("support-default"))
    model = runs[2]["post"]
    assert model["id"] == model["trace_id"] == "<id>" and model["start_time"] == "<time>"
    assert model["dotted_order"].count("<time><id>") == 3
    assert runs[2]["post.extra"]["metadata"]["ls_model_name"] == "gpt-4o-2024-08-06"
    assert ls.mask(1794239975929) == "<time>" and ls.mask(420) == 420
    assert ls.mask("e826c1dfe84446c2") == "<id>" and ls.mask("kb-112") == "kb-112"


def test_the_opentelemetry_mode_exports_the_same_runs_as_spans_beside_them():
    made = requests("support-otel")
    assert {request["path"] for request in made} == {ls.RUNS_PATH, ls.OTLP_PATH}
    for request in ls.otlp_requests(made):
        for resource in request["resourceSpans"]:
            for scope in resource["scopeSpans"]:
                for span in scope["spans"]:
                    assert re.fullmatch(r"[0-9a-f]{32}", span["traceId"])
                    assert re.fullmatch(r"[0-9a-f]{16}", span["spanId"])
                    assert isinstance(span["kind"], int)
    spans = ls.masked_spans(made)
    assert sorted(span["name"] for span in spans) == sorted(name for _, name in SUPPORT)
    roots = [span["name"] for span in spans if span["root"]]
    assert roots == ["support"]
    resource = {item["key"]: item["value"] for item in spans[0]["resource"]}
    assert resource["langsmith.internal_provider"] == {"boolValue": True}
    # Its own run types, and its own spelling of the GenAI operation: a retriever is "embeddings", a chain "chain".
    kinds = {
        span["name"]: {item["key"]: item["value"].get("stringValue") for item in span["attributes"]} for span in spans
    }
    assert kinds["HelpCentre"]["langsmith.span.kind"] == "retriever"
    assert kinds["HelpCentre"]["gen_ai.operation.name"] == "embeddings"
    assert kinds["support"]["gen_ai.operation.name"] == "chain"

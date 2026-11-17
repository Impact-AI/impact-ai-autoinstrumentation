# SPDX-License-Identifier: Apache-2.0

"""LangSmith beside the package (Python).

LangSmith's default tracing uses LangChain's callbacks, its traceable decorator and its client wrappers; it never takes
the process's OpenTelemetry provider. So the package starts beside it and traces the same workload, and LangSmith's
backend (the recording mock `theirs`) receives the same runs as without the package, once generated ids and times are
masked. LangSmith's OpenTelemetry mode does take the provider and exports OTLP itself: there the package stands down,
and LangSmith receives what it received before, its runs and its spans, a root still a root.

Each test runs the same workload twice, without the package and with it, and compares what LangSmith received.
"""

from __future__ import annotations

import langsmith_runs as ls
import pytest
from test_injected_apps import wait_ready

from conftest import app_image, require_images
from containers import http, wait_until

IMAGE = app_image("3.12", "langsmith")
UVICORN = ["uvicorn", "app_langsmith:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
# The operation of each span the package writes for a scenario: LangChain and LangGraph through their callbacks, the
# wrapped OpenAI client through the OpenAI instrumentation.
TRACED = {
    "langchain": {"invoke_agent", "chat", "execute_tool"},
    "langgraph": {"invoke_agent", "chat", "execute_tool"},
    "langgraph-graph": {"invoke_workflow", "chat", "execute_tool"},
    "traceable": {"chat"},
    "support": {"invoke_agent", "chat", "execute_tool", "retrieval"},
}


def langsmith_env(theirs, **extra) -> dict[str, str]:
    return {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_ENDPOINT": theirs.internal_url,
        "LANGSMITH_API_KEY": "lsv2_pt_planted",
        "LANGSMITH_PROJECT": "support",
        **extra,
    }


def setup_reports(mock):
    return [span for span in mock.spans() if span["name"] == "impact.setup"]


def received(start_app, mock, theirs, scenario: str, *, inject: bool, env: dict[str, str]) -> list[dict]:
    """Run the scenario once, let LangSmith send everything, and return what its backend received.

    The workload without the package is stopped once LangSmith has sent everything, so that nothing of it reaches the
    next run; the one with the package keeps running until the test has read the package's spans.
    """
    mock.reset()
    theirs.reset()
    app = start_app(IMAGE, UVICORN, inject=inject, env=env)
    wait_ready(app)
    status, answer = http("POST", f"{app.url}/langsmith/{scenario}", {}, timeout=120)
    assert status == 200, (answer, app.logs())
    assert http("POST", f"{app.url}/flush", {}, timeout=60)[0] == 200
    if not inject:
        app.stop()
    return theirs.requests()


@pytest.mark.langsmith
@pytest.mark.parametrize("scenario", sorted(TRACED))
def test_default_tracing_receives_the_same_runs_beside_the_package(start_app, mock, theirs, scenario):
    require_images(IMAGE)
    env = langsmith_env(theirs)
    without = ls.masked_runs(received(start_app, mock, theirs, scenario, inject=False, env=env))
    assert without, "LangSmith received no run"
    with_package = received(start_app, mock, theirs, scenario, inject=True, env=env)
    assert ls.masked_runs(with_package) == without
    # The package started beside LangSmith and traced the workload.
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    (report,) = setup_reports(mock)
    assert report["attrs"]["impact.setup.state"] == "active", report["attrs"]
    # It names LangSmith and its version, and that it started beside it; never a setting's value.
    assert report["attrs"]["impact.setup.tool"] == "langsmith"
    assert report["attrs"]["impact.setup.tool.version"] == "0.14.17"
    assert report["attrs"]["impact.setup.alongside"] is True
    assert "impact.setup.exporter" not in report["attrs"]
    assert "lsv2_pt_planted" not in str(report["attrs"])
    wait_until(
        lambda: TRACED[scenario] <= {span["attrs"].get("gen_ai.operation.name") for span in mock.spans()},
        timeout=30,
        message=lambda: f"{TRACED[scenario]} in {[s['name'] for s in mock.spans()]}",
    )


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "setting",
    [
        {"LANGSMITH_OTEL_ENABLED": "true"},  # hybrid: LangSmith's runs and its spans
        {"LANGSMITH_TRACING_MODE": "otel"},  # its spans only
        {"LANGSMITH_OTEL_ONLY": "true"},  # its spans only, by the older switch
    ],
    ids=lambda setting: next(iter(setting)),
)
def test_the_opentelemetry_mode_stands_the_package_down_and_langsmith_receives_what_it_did(
    start_app, mock, theirs, setting
):
    require_images(IMAGE)
    env = langsmith_env(theirs, **setting)
    without = received(start_app, mock, theirs, "support", inject=False, env=env)
    spans = ls.masked_spans(without)
    assert [span["name"] for span in spans if span["root"]] == ["support"]
    with_package = received(start_app, mock, theirs, "support", inject=True, env=env)
    assert ls.masked_spans(with_package) == spans
    assert ls.masked_runs(with_package) == ls.masked_runs(without)
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    (report,) = setup_reports(mock)
    assert report["attrs"]["impact.setup.state"] == "pre-existing"
    assert report["attrs"]["impact.setup.detail"] == f"{next(iter(setting))} (set by the workload)"
    # It names LangSmith exporting OTLP over HTTP, and that it did not start beside it.
    assert report["attrs"]["impact.setup.tool"] == "langsmith"
    assert report["attrs"]["impact.setup.exporter"] == "otlp-http"
    assert report["attrs"]["impact.setup.alongside"] is False
    assert [span["name"] for span in mock.spans()] == ["impact.setup"]

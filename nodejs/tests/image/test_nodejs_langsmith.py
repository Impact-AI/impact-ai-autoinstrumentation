# SPDX-License-Identifier: Apache-2.0

"""LangSmith beside the package (Node.js).

LangSmith's default tracing in Node.js (LangChain's callbacks, traceable() and wrapOpenAI) never touches the
OpenTelemetry API, so the package starts beside it and LangSmith's backend (the recording mock `theirs`) receives the
same runs as without the package, once generated ids and times are masked. The package traces the model calls;
LangChain's and LangGraph's agent and tool steps are not traced by the package in Node.js yet, so those workloads are
not at Full here.

LangSmith's OpenTelemetry mode registers the process's provider (initializeOTEL()). There the package stands down: with
it running, the application's own @opentelemetry/api could not register LangSmith's provider, and LangSmith would
receive nothing. With it stood down, LangSmith receives what it received before.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from containers import http, wait_until
from nodejs_support import app_image, llm_spans, only, require_images, setup_reports, wait_ready

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "python" / "tests"))
import langsmith_runs as ls  # noqa: E402

NODE = "22"
COMMAND = ["node", "langsmith.cjs"]
DIRECTORY = "/apps/langsmith"
# The model calls of each scenario, which the package traces.
MODEL_CALLS = {"langchain": 2, "langgraph": 2, "traceable": 1}


def langsmith_env(theirs, **extra) -> dict[str, str]:
    return {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_ENDPOINT": theirs.internal_url,
        "LANGSMITH_API_KEY": "lsv2_pt_planted",
        "LANGSMITH_PROJECT": "support",
        **extra,
    }


def received(start_app, mock, theirs, scenario: str, *, inject: bool, env: dict[str, str]) -> list[dict]:
    """Run the scenario once, let LangSmith send everything, and return what its backend received.

    The workload without the package is stopped once LangSmith has sent everything, so that nothing of it reaches the
    next run; the one with the package keeps running until the test has read the package's spans.
    """
    mock.reset()
    theirs.reset()
    app = start_app(NODE, COMMAND, directory=DIRECTORY, inject=inject, env=env)
    wait_ready(app)
    status, answer = http("POST", f"{app.url}/langsmith/{scenario}", {}, timeout=120)
    assert status == 200, (answer, app.logs())
    assert http("POST", f"{app.url}/flush", {}, timeout=60)[0] == 200
    if not inject:
        app.stop()
    return theirs.requests()


@pytest.mark.langsmith
@pytest.mark.parametrize("scenario", sorted(MODEL_CALLS))
def test_default_tracing_receives_the_same_runs_beside_the_package(start_app, mock, theirs, scenario):
    require_images(app_image(NODE))
    env = langsmith_env(theirs)
    without = ls.masked_runs(received(start_app, mock, theirs, scenario, inject=False, env=env))
    assert without, "LangSmith received no run"
    with_package = received(start_app, mock, theirs, scenario, inject=True, env=env)
    assert ls.masked_runs(with_package) == without
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "active", report["attrs"]
    # It names LangSmith and its version, and that it started beside it; never a setting's value.
    assert report["attrs"]["impact.setup.tool"] == "langsmith"
    assert report["attrs"]["impact.setup.tool.version"] == "0.11.8"
    assert report["attrs"]["impact.setup.alongside"] is True
    assert "impact.setup.exporter" not in report["attrs"]
    assert "lsv2_pt_planted" not in str(report["attrs"])
    wait_until(
        lambda: len(llm_spans(mock)) == MODEL_CALLS[scenario],
        timeout=30,
        message=lambda: f"{MODEL_CALLS[scenario]} model calls in {[s['name'] for s in mock.spans()]}",
    )


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "setting",
    [{"LANGSMITH_OTEL_ENABLED": "true"}, {"LANGSMITH_TRACING_MODE": "otel"}],
    ids=lambda setting: next(iter(setting)),
)
def test_the_opentelemetry_mode_stands_the_package_down_and_langsmith_receives_what_it_did(
    start_app, mock, theirs, setting
):
    require_images(app_image(NODE))
    env = langsmith_env(theirs, **setting)
    # In this mode LangSmith exports traceable functions and wrapped clients; its LangChain runs it does not export.
    spans = ls.masked_spans(received(start_app, mock, theirs, "traceable", inject=False, env=env))
    assert [span["name"] for span in spans if span["root"]] == ["plan_trip"]
    assert ls.masked_spans(received(start_app, mock, theirs, "traceable", inject=True, env=env)) == spans
    wait_until(lambda: setup_reports(mock), timeout=30, message="the setup report")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "pre-existing"
    assert report["attrs"]["impact.setup.detail"] == f"{next(iter(setting))} (set by the workload)"
    # It names LangSmith exporting OTLP over HTTP, and that it did not start beside it.
    assert report["attrs"]["impact.setup.tool"] == "langsmith"
    assert report["attrs"]["impact.setup.exporter"] == "otlp-http"
    assert report["attrs"]["impact.setup.alongside"] is False
    assert [span["name"] for span in mock.spans()] == ["impact.setup"]

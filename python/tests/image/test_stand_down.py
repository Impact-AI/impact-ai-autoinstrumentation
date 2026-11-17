# SPDX-License-Identifier: Apache-2.0

"""Stand down: the workload starts a tracer of its own and is annotated anyway. The package does not start, the
workload's own spans reach its own endpoint as before, and the setup report says what was found."""

from __future__ import annotations

import pytest
from test_injected_apps import wait_ready

from conftest import app_image, require_images
from containers import http, protobuf_spans, wait_until

UVICORN = ["uvicorn", "app_asgi:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104


def setup_reports(mock):
    return [s for s in mock.spans() if s["name"] == "impact.setup"]


def assert_stood_down(app, mock, detail_starts_with: str, tool: str, exporter: str | None = None) -> None:
    wait_until(lambda: setup_reports(mock), timeout=30, message=lambda: f"the setup report\n{app.logs()}")
    (report,) = setup_reports(mock)
    assert report["attrs"]["impact.setup.state"] == "pre-existing"
    assert report["attrs"]["impact.setup.detail"].startswith(detail_starts_with), report["attrs"]
    # It names the tool it found and its exporter kind when the settings show it, and that it did not start beside it.
    assert report["attrs"]["impact.setup.tool"] == tool, report["attrs"]
    assert report["attrs"].get("impact.setup.exporter") == exporter, report["attrs"]
    assert report["attrs"]["impact.setup.alongside"] is False
    # A stood-down workload still says which AI libraries it has: Cover your AI shows them next to who owns the tracing.
    frameworks = report["attrs"]["impact.setup.frameworks"]
    assert any(entry.startswith("openai ") for entry in frameworks) and any(
        e.startswith("anthropic ") for e in frameworks
    )
    assert report["attrs"]["impact.setup.untraced"] == []
    # Nothing but the report reaches impact.ai.
    assert [s["name"] for s in mock.spans()] == ["impact.setup"]
    # Nothing of ours is left in the process.
    status, runtime = http("GET", f"{app.url}/debug/runtime")
    assert status == 200
    assert not [p for p in runtime["sys_path"] if "otel-auto-instrumentation-python" in p]
    assert "otel-auto-instrumentation-python" not in (runtime["pythonpath"] or "")


@pytest.mark.image
def test_an_entry_point_that_runs_opentelemetry_instrument(start_app, mock, theirs):
    require_images(app_image("3.12", "otel"))
    app = start_app(
        app_image("3.12", "otel"),
        ["opentelemetry-instrument", *UVICORN],
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": theirs.internal_url,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME": "theirs",
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "none",
        },
    )
    wait_ready(app)
    assert http("POST", f"{app.url}/chat/openai", {"prompt": "hi there"})[1] == {"answer": "Hello from the stub."}
    assert_stood_down(app, mock, "opentelemetry-instrument (launcher)", "opentelemetry", "otlp-http")
    wait_until(
        lambda: any(s["name"] == "POST /chat/openai" for s in protobuf_spans(theirs)),
        timeout=30,
        message=lambda: f"the workload's own server span among {[s['name'] for s in protobuf_spans(theirs)]}",
    )


@pytest.mark.image
@pytest.mark.tracers
def test_datadog_started_in_code(start_app, mock, theirs):
    require_images(app_image("3.12", "ddtrace"))
    app = start_app(
        app_image("3.12", "ddtrace"),
        ["uvicorn", "app_ddtrace:app", "--host", "0.0.0.0", "--port", "8000"],  # noqa: S104
        env={"DD_TRACE_AGENT_URL": theirs.internal_url, "DD_SERVICE": "theirs", "DD_TRACE_STARTUP_LOGS": "false"},
    )
    wait_ready(app)
    assert http("POST", f"{app.url}/chat/openai", {"prompt": "hi there"})[1] == {"answer": "Hello from the stub."}
    assert_stood_down(app, mock, "ddtrace ", "datadog")
    wait_until(
        lambda: [r for r in theirs.requests() if r["path"].startswith("/v0.")],
        timeout=30,
        message="traces sent to the Datadog agent",
    )


@pytest.mark.image
@pytest.mark.tracers
def test_traceloop_started_in_code(start_app, mock, theirs):
    require_images(app_image("3.12", "traceloop"))
    app = start_app(
        app_image("3.12", "traceloop"),
        ["uvicorn", "app_traceloop:app", "--host", "0.0.0.0", "--port", "8000"],  # noqa: S104
        env={"THEIRS_URL": theirs.internal_url, "TRACELOOP_TELEMETRY": "false"},
    )
    wait_ready(app)
    assert http("POST", f"{app.url}/chat/openai", {"prompt": "hi there"})[1] == {"answer": "Hello from the stub."}
    assert_stood_down(app, mock, "traceloop-sdk ", "traceloop")
    wait_until(
        lambda: any("openai" in s["name"] for s in protobuf_spans(theirs)),
        timeout=30,
        message=lambda: f"Traceloop's own openai span among {[s['name'] for s in protobuf_spans(theirs)]}",
    )


@pytest.mark.image
def test_an_opentelemetry_pipeline_started_in_code(start_app, mock, theirs):
    require_images(app_image("3.12", "otel"))
    app = start_app(
        app_image("3.12", "otel"),
        ["uvicorn", "app_own_otel:app", "--host", "0.0.0.0", "--port", "8000"],  # noqa: S104
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": theirs.internal_url,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME": "theirs",
        },
    )
    wait_ready(app)
    assert http("POST", f"{app.url}/chat/openai", {"prompt": "hi there"})[1] == {"answer": "Hello from the stub."}
    assert_stood_down(app, mock, "OTEL_EXPORTER_OTLP_ENDPOINT", "opentelemetry", "otlp-http")
    wait_until(
        lambda: any(s["name"].startswith("POST /chat/openai") for s in protobuf_spans(theirs)),
        timeout=30,
        message=lambda: f"the workload's own spans among {[s['name'] for s in protobuf_spans(theirs)]}",
    )

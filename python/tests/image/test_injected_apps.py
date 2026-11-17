# SPDX-License-Identifier: Apache-2.0

"""Coverage (image side): a Python workload that calls OpenAI, Anthropic or Bedrock Converse is covered by the payload
alone, under uvicorn and gunicorn, on Python 3.11 to 3.14. Prompt and answer arrive on the span (mock providers).
"""

from __future__ import annotations

import json

import pytest

from conftest import app_image, require_images
from containers import http, wait_until

PYTHONS = ["3.11", "3.12", "3.13", "3.14"]
SERVERS = {
    "uvicorn": (["uvicorn", "app_asgi:app", "--host", "0.0.0.0", "--port", "8000"], {"fastapi", "starlette"}),  # noqa: S104
    "gunicorn": (
        # /dev/shm and no control socket: gunicorn 26 in a container without a writable home or working directory.
        ["gunicorn", "app_wsgi:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--worker-tmp-dir", "/dev/shm"]  # noqa: S108
        + ["--no-control-socket"],
        {"flask"},
    ),  # noqa: S104
}
PROVIDERS = {
    "openai": {"model": "gpt-4o-mini", "instrumentation": "openai"},
    "anthropic": {"model": "claude-stub", "instrumentation": "anthropic"},
    "bedrock": {"model": "claude-3-haiku-20240307-v1:0", "instrumentation": "boto3"},
    # A returns assistant calls amazon.nova-micro-v1:0 through Converse; the span carries the model without the
    # vendor prefix, as it does for Claude above.
    "nova": {"model": "nova-micro-v1:0", "instrumentation": "boto3"},
}
PROMPT, ANSWER = "hi there", "Hello from the stub."


def wait_ready(app):
    wait_until(
        lambda: http("GET", f"{app.url}/healthz")[0] == 200, timeout=90, message=lambda: f"app to start\n{app.logs()}"
    )


def llm_spans(mock):
    return [s for s in mock.spans() if any(k.startswith("gen_ai.") for k in s["attrs"]) and s["name"] != "impact.setup"]


def reported_with_their_requests(mock, models):
    """Each model's span has arrived together with the request (server) span it hangs under. A span is exported when it
    ends: the LLM call a moment before the request that made it, and the exporter runs on its own schedule."""
    spans = mock.spans()
    servers = {s["spanId"] for s in spans if s.get("kind") == 2}  # SERVER
    by_model = {
        s["attrs"].get("gen_ai.request.model"): s
        for s in spans
        if any(k.startswith("gen_ai.") for k in s["attrs"]) and s["name"] != "impact.setup"
    }
    return models <= by_model.keys() and all(by_model[m].get("parentSpanId") in servers for m in models)


@pytest.mark.image
@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize("python", PYTHONS)
def test_llm_calls_arrive_with_content_provider_model_tokens_and_placement(start_app, mock, python, server):
    require_images(app_image(python))
    command, frameworks = SERVERS[server]
    app = start_app(app_image(python), command)
    wait_ready(app)

    for provider in PROVIDERS:
        status, body = http("POST", f"{app.url}/chat/{provider}", {"prompt": PROMPT})
        assert status == 200, (provider, body, app.logs())
        assert body == {"answer": ANSWER}  # the application behaves exactly as without injection
    for _ in range(3):
        http("GET", f"{app.url}/healthz")

    # Wait for each provider's own span and for the request it hangs under, not just any three: workers export on their
    # own schedule, and the request span of the last call is exported after the call's own.
    models = {expected["model"] for expected in PROVIDERS.values()}
    wait_until(
        lambda: reported_with_their_requests(mock, models),
        timeout=30,
        message=lambda: (
            f"an LLM span for each of {sorted(models)}, with its request span: "
            f"{[(s['name'], s.get('kind'), s['spanId'], s.get('parentSpanId')) for s in mock.spans()]}\n{app.logs()}"
        ),
    )
    spans = mock.spans()
    by_model = {s["attrs"].get("gen_ai.request.model"): s for s in llm_spans(mock)}
    for provider, expected in PROVIDERS.items():
        span = by_model[expected["model"]]
        attrs = span["attrs"]
        assert attrs["gen_ai.usage.input_tokens"] == 7, provider
        assert attrs["gen_ai.usage.output_tokens"] == 5, provider
        assert PROMPT in attrs["gen_ai.input.messages"], provider
        assert ANSWER in attrs["gen_ai.output.messages"], provider
        # Placement: the workload's own k8s.* attributes, set by the Operator, ride on the span's resource.
        assert span["resource"]["k8s.namespace.name"] == "shop"
        assert span["resource"]["k8s.deployment.name"] == "support-assistant"
        assert span["resource"]["service.name"] == "support-assistant"

    # The LLM span hangs under the request span, so a trace shows the call in its request.
    roots = {s["spanId"]: s for s in spans if s.get("kind") == 2}  # SERVER
    assert roots, [s["name"] for s in spans]
    for span in by_model.values():
        assert span.get("parentSpanId") in roots, (span["name"], [s["name"] for s in spans])
        assert roots[span["parentSpanId"]]["traceId"] == span["traceId"]

    # Health probes are not traced.
    assert not [s for s in spans if "healthz" in json.dumps(s["attrs"]) or "/healthz" in s["name"]]

    setup = next(s for s in spans if s["name"] == "impact.setup")
    assert setup["attrs"]["impact.setup.state"] == "active"
    instrumented = set(setup["attrs"]["impact.setup.instrumentations"].split(","))
    assert instrumented >= {"openai", "anthropic", "boto3"} | frameworks


@pytest.mark.image
def test_no_provider_span_leaks_from_the_sdks_own_telemetry(start_app, mock):
    # anthropic's SDK emits spans of its own through the global tracer. The package never sets one, so only the
    # instrumentation's span exists for a call: one span per LLM call, not two.
    require_images(app_image("3.12"))
    app = start_app(app_image("3.12"), SERVERS["uvicorn"][0])
    wait_ready(app)
    http("POST", f"{app.url}/chat/anthropic", {"prompt": PROMPT})
    wait_until(lambda: llm_spans(mock), timeout=30, message="the anthropic span")
    assert [s["scope"] for s in llm_spans(mock)].count("com.anthropic.sdk.python") == 0
    assert len([s for s in llm_spans(mock) if s["attrs"].get("gen_ai.request.model") == "claude-stub"]) == 1


@pytest.mark.image
def test_content_stays_off_the_span_when_the_workload_asks_for_no_content(start_app, mock):
    require_images(app_image("3.12"))
    app = start_app(
        app_image("3.12"),
        SERVERS["uvicorn"][0],
        env={"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT"},
    )
    wait_ready(app)
    for provider in PROVIDERS:
        http("POST", f"{app.url}/chat/{provider}", {"prompt": PROMPT})
    wait_until(lambda: len(llm_spans(mock)) >= len(PROVIDERS), timeout=30, message="an LLM span for each provider")
    for span in llm_spans(mock):
        text = json.dumps(span["attrs"])
        assert PROMPT not in text and ANSWER not in text, span["name"]
        assert span["attrs"].get("gen_ai.usage.input_tokens") == 7


@pytest.mark.image
def test_the_application_is_not_changed(start_app, mock):
    """The same image, same code, without the payload: identical answers (the row's command changes nothing else)."""
    require_images(app_image("3.12"))
    plain = start_app(app_image("3.12"), SERVERS["uvicorn"][0], inject=False)
    wait_ready(plain)
    status, body = http("POST", f"{plain.url}/chat/openai", {"prompt": PROMPT})
    assert (status, body) == (200, {"answer": ANSWER})
    assert [s for s in mock.spans()] == []

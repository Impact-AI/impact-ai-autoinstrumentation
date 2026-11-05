# SPDX-License-Identifier: Apache-2.0

"""A returns assistant (`return-reasons`), covered by the payload alone.

Starlette on uvicorn, a poller thread, plain boto3 `bedrock-runtime` Converse on `amazon.nova-micro-v1:0`, no
OpenTelemetry of its own, and nothing else in its environment: no FastAPI, no OpenAI or Anthropic SDK, not even
`packaging`. The image is the real service's in miniature (testing/apps/app_starlette.py) at the real service's
versions: Python 3.13 (the digest of its base image), boto3 and botocore 1.43.123, starlette 1.10.0, uvicorn 0.54.0.

Two kinds of call are made, because the real service makes the first and a Starlette app would make the second: the
poller's, from a background thread (no request, so the Converse span is a trace root of its own), and one from a
request (the Converse span hangs under the Starlette server span).
"""

from __future__ import annotations

import json

import pytest
from test_injected_apps import wait_ready

from conftest import app_image, require_images
from containers import http, wait_until

COMMAND = ["uvicorn", "app_starlette:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]  # noqa: S104
MODEL, SPAN_MODEL = "amazon.nova-micro-v1:0", "nova-micro-v1:0"  # the span carries the model without the vendor prefix
ANSWER = "Hello from the stub."
TEXT = "The image is blurry at high power."
PLACEMENT = {
    "k8s.namespace.name": "astronomy-shop",
    "k8s.deployment.name": "return-reasons",
    "service.name": "return-reasons",
}


def returns_env(mock, **extra: str) -> dict[str, str]:
    return {
        "BEDROCK_MODEL_ID": MODEL,
        "AWS_REGION": "us-east-1",
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": mock.internal_url,  # boto3 reads it: the application's code is untouched
        "OTEL_RESOURCE_ATTRIBUTES": (
            "k8s.container.name=return-reasons,k8s.deployment.name=return-reasons,k8s.namespace.name=astronomy-shop,"
            "k8s.node.name=node-1,k8s.pod.name=return-reasons-5f9c-x2,k8s.replicaset.name=return-reasons-5f9c,"
            "service.instance.id=astronomy-shop.return-reasons-5f9c-x2.return-reasons,service.namespace=astronomy-shop"
        ),
        **extra,
    }


def nova_spans(mock) -> list[dict]:
    return [s for s in mock.spans() if s["attrs"].get("gen_ai.request.model") == SPAN_MODEL]


def server_span(mock, name: str) -> dict | None:
    return next((s for s in mock.spans() if s.get("kind") == 2 and s["name"] == name), None)


@pytest.mark.image
def test_the_returns_assistant_is_covered_with_content_model_tokens_and_placement(start_app, mock):
    require_images(app_image("3.13", "starlette"))
    app = start_app(app_image("3.13", "starlette"), COMMAND, env=returns_env(mock))
    wait_ready(app)

    # The poller's call: a Converse span of its own trace, no request around it.
    (poll,) = wait_until(lambda: nova_spans(mock)[:1], timeout=60, message=lambda: f"the poller's span\n{app.logs()}")
    attrs = poll["attrs"]
    assert poll["name"] == f"chat {SPAN_MODEL}"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "aws.bedrock"
    assert attrs["gen_ai.usage.input_tokens"] == 7
    assert attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["gen_ai.usage.total_tokens"] == 12
    assert attrs["gen_ai.request.max_tokens"] == 300
    assert attrs["gen_ai.request.temperature"] == 0.0
    assert TEXT in attrs["gen_ai.input.messages"]  # the customer's words, on the span
    assert "pinhole" in attrs["gen_ai.input.messages"]  # and the examples that came with them
    assert "Acme Outfitters" in attrs["gen_ai.system_instructions"]
    assert ANSWER in attrs["gen_ai.output.messages"]
    assert not poll.get("parentSpanId")
    for key, value in PLACEMENT.items():
        assert poll["resource"][key] == value, key

    # A request: the Starlette server span is the root of its trace, with the workload's placement.
    assert http("GET", f"{app.url}/v1/summary")[0] == 200
    root = wait_until(lambda: server_span(mock, "GET /v1/summary"), timeout=30, message=lambda: app.logs())
    assert root["attrs"]["http.route"] == "/v1/summary"
    assert root["attrs"]["http.status_code"] == 200
    assert not root.get("parentSpanId")
    for key, value in PLACEMENT.items():
        assert root["resource"][key] == value, key

    # A model call made from a request hangs under that request's server span, in one trace.
    status, body = http("POST", f"{app.url}/v1/classify", {}, timeout=30)
    assert (status, body) == (200, {"answer": ANSWER}), app.logs()
    classify = wait_until(lambda: server_span(mock, "POST /v1/classify"), timeout=30, message=lambda: app.logs())
    under = wait_until(
        lambda: [s for s in nova_spans(mock) if s.get("parentSpanId") == classify["spanId"]],
        timeout=30,
        message=lambda: f"a Converse span under the request\n{app.logs()}",
    )
    assert under[0]["traceId"] == classify["traceId"]

    # Probes are not traced; the report names what was armed and finds no AI library besides boto3.
    for probe in ("/healthz", "/readyz"):
        assert http("GET", f"{app.url}{probe}")[0] == 200
    spans = mock.spans()
    assert not [s for s in spans if "/healthz" in json.dumps(s["attrs"]) or "/readyz" in json.dumps(s["attrs"])]
    setup = next(s for s in spans if s["name"] == "impact.setup")
    assert setup["attrs"]["impact.setup.state"] == "active"
    assert set(setup["attrs"]["impact.setup.instrumentations"].split(",")) == {"boto3", "starlette"}
    assert setup["attrs"]["impact.setup.frameworks"] == []
    assert setup["attrs"]["impact.setup.untraced"] == []
    assert "Traceback" not in app.logs()


@pytest.mark.image
def test_the_returns_assistant_keeps_its_customers_words_off_the_span_when_asked(start_app, mock):
    require_images(app_image("3.13", "starlette"))
    app = start_app(
        app_image("3.13", "starlette"),
        COMMAND,
        env=returns_env(mock, OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="NO_CONTENT"),
    )
    wait_ready(app)
    (poll,) = wait_until(lambda: nova_spans(mock)[:1], timeout=60, message=lambda: app.logs())
    text = json.dumps(poll["attrs"])
    for secret in (TEXT, "pinhole", "Acme Outfitters", ANSWER):
        assert secret not in text, secret
    assert poll["attrs"]["gen_ai.usage.input_tokens"] == 7


@pytest.mark.image
def test_the_returns_assistant_runs_the_same_without_the_payload(start_app, mock):
    require_images(app_image("3.13", "starlette"))
    plain = start_app(app_image("3.13", "starlette"), COMMAND, env=returns_env(mock), inject=False)
    wait_ready(plain)
    assert http("POST", f"{plain.url}/v1/classify", {}, timeout=30) == (200, {"answer": ANSWER})
    assert mock.spans() == []

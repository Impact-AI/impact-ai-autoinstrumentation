# SPDX-License-Identifier: Apache-2.0

"""Business context, image side: an injected workload whose handler puts the user, the session and the use case in
OpenTelemetry baggage gets them on the model call's span, through the payload alone, as the mock ingest receives it.

The fixture's /chat/openai/in-context sets user.id, session.id and impact.use_case, and one key the package must not
read (tenant.plan), around the same OpenAI call as /chat/openai. The request also carries a baggage header from the
caller with values of its own: the application's, set after the request arrived, are the ones on the span.

A caller's baggage header sets none of the four keys by default (/chat/openai, where the application sets none);
with IMPACT_TRUST_CALLER_BAGGAGE=true, what the chart sets from the pod's annotation impact.ai/trust-caller-baggage,
the caller's values arrive.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pytest

from conftest import app_image, require_images
from containers import http, wait_until

UVICORN = ["uvicorn", "app_asgi:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104
CONTEXT = {"user": "u-1842", "session": "s-77", "use_case": "returns assistant"}


def post(url: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(  # noqa: S310 - the fixture's own address
        url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode())


def model_span(mock):
    spans = [s for s in mock.spans() if s["attrs"].get("gen_ai.request.model") == "gpt-4o-mini"]
    return spans[0] if spans else None


@pytest.mark.image
@pytest.mark.parametrize("python", ["3.11", "3.14"])
def test_the_context_the_application_sets_arrives_on_the_model_call(start_app, mock, python):
    require_images(app_image(python))
    app = start_app(app_image(python), UVICORN)
    wait_until(
        lambda: http("GET", f"{app.url}/healthz")[0] == 200, timeout=90, message=lambda: f"app to start\n{app.logs()}"
    )

    forged = "user.id=someone-else,impact.use_case=forged,tenant.plan=forged"
    status, body = post(
        f"{app.url}/chat/openai/in-context?{urllib.parse.urlencode(CONTEXT)}",
        {"prompt": "hi there"},
        {"baggage": forged},
    )
    assert (status, body) == (200, {"answer": "Hello from the stub."}), app.logs()  # the application is unchanged

    span = wait_until(lambda: model_span(mock), timeout=30, message=lambda: f"the model call's span\n{app.logs()}")
    attrs = span["attrs"]
    assert attrs["user.id"] == "u-1842"
    assert attrs["session.id"] == "s-77"
    assert attrs["impact.use_case"] == "returns assistant"
    assert "tenant.plan" not in attrs  # only the four keys, never the rest of the baggage
    assert "gen_ai.conversation.id" not in attrs  # the application set none
    assert not [key for key, value in attrs.items() if value == "forged"]


FORGED = "user.id=someone-else,session.id=forged,impact.use_case=forged,tenant.plan=forged"


@pytest.mark.image
@pytest.mark.parametrize(
    ("trust", "expected"),
    [
        ({}, {}),
        ({"IMPACT_TRUST_CALLER_BAGGAGE": ""}, {}),  # the pod has no annotation: the downward API gives an empty value
        (
            {"IMPACT_TRUST_CALLER_BAGGAGE": "true"},
            {"user.id": "someone-else", "session.id": "forged", "impact.use_case": "forged"},
        ),
    ],
    ids=["default", "no-annotation", "trusted"],
)
def test_a_callers_baggage_reaches_the_span_only_where_the_workload_trusts_its_callers(
    start_app, mock, trust, expected
):
    python = "3.14"
    require_images(app_image(python))
    app = start_app(app_image(python), UVICORN, env=trust)
    wait_until(
        lambda: http("GET", f"{app.url}/healthz")[0] == 200, timeout=90, message=lambda: f"app to start\n{app.logs()}"
    )

    status, body = post(f"{app.url}/chat/openai", {"prompt": "hi there"}, {"baggage": FORGED})
    assert (status, body) == (200, {"answer": "Hello from the stub."}), app.logs()

    span = wait_until(lambda: model_span(mock), timeout=30, message=lambda: f"the model call's span\n{app.logs()}")
    attrs = span["attrs"]
    assert {key: attrs[key] for key in ("user.id", "session.id", "impact.use_case") if key in attrs} == expected
    assert "tenant.plan" not in attrs  # never anything but the four keys

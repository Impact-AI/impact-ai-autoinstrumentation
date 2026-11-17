# SPDX-License-Identifier: Apache-2.0

"""Business context, image side: a Node.js workload whose handler puts the user, the session and the use case in
OpenTelemetry baggage gets them on the model call's span, through the payload alone, as the mock ingest receives it.

The fixture (testing/apps/nodejs/src/context.cjs) depends on @opentelemetry/api and adds the three keys, and one the
package must not read (tenant.plan), to the baggage of the request around an OpenAI call (made with the kit's own
provider) or an Anthropic call (its SDK's spans, made with the provider the kit registers globally). The package
registers W3C trace context only, so a baggage header from the caller is not read: a key the application does not set
is not on the span. With IMPACT_TRUST_CALLER_BAGGAGE=true, what the chart sets from the pod's annotation
impact.ai/trust-caller-baggage, it registers W3C baggage too, and the caller's value arrives where the application
sets none; the application's own still wins.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pytest

from nodejs_support import ANSWER, ANTHROPIC_MODEL, OPENAI_MODEL, PROMPT, app_image, require_images, settle, wait_ready

CONTEXT = {"user": "u-1842", "use_case": "returns assistant"}
MODELS = {"openai": OPENAI_MODEL, "anthropic": ANTHROPIC_MODEL}


def post(url: str, body: dict, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(  # noqa: S310 - the fixture's own address
        url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode())


def model(span: dict) -> str | None:
    return span["attrs"].get("llm.model_name") or span["attrs"].get("gen_ai.request.model")


@pytest.mark.nodejs
@pytest.mark.parametrize("provider", MODELS)
def test_the_context_the_application_sets_arrives_on_the_model_call(start_app, mock, provider):
    node = "24"
    require_images(app_image(node))
    app = start_app(node, ["node", "context.cjs"], directory="/apps/context")
    wait_ready(app)

    status, body = post(
        f"{app.url}/chat/{provider}/in-context?{urllib.parse.urlencode(CONTEXT)}",
        {"prompt": PROMPT},
        {"baggage": "user.id=forged,session.id=forged,tenant.plan=forged"},
    )
    assert (status, body) == (200, {"answer": ANSWER}), app.logs()  # the application is unchanged

    (span,) = settle(mock, 1)
    assert model(span) == MODELS[provider], span["attrs"]
    attrs = span["attrs"]
    assert attrs["user.id"] == "u-1842"
    assert attrs["impact.use_case"] == "returns assistant"
    assert "session.id" not in attrs  # the application set none, and the caller's header is not read
    assert "tenant.plan" not in attrs  # only the four keys, never the rest of the baggage
    assert not [key for key, value in attrs.items() if value == "forged"]


@pytest.mark.nodejs
def test_a_workload_that_trusts_its_callers_gets_their_values_where_it_sets_none(start_app, mock):
    node = "24"
    require_images(app_image(node))
    app = start_app(
        node, ["node", "context.cjs"], directory="/apps/context", env={"IMPACT_TRUST_CALLER_BAGGAGE": "true"}
    )
    wait_ready(app)

    status, body = post(
        f"{app.url}/chat/openai/in-context?{urllib.parse.urlencode(CONTEXT)}",
        {"prompt": PROMPT},
        {"baggage": "user.id=forged,session.id=from-the-gateway,tenant.plan=forged"},
    )
    assert (status, body) == (200, {"answer": ANSWER}), app.logs()

    (span,) = settle(mock, 1)
    assert model(span) == OPENAI_MODEL, span["attrs"]
    attrs = span["attrs"]
    assert attrs["user.id"] == "u-1842"  # the application's own value wins
    assert attrs["impact.use_case"] == "returns assistant"
    assert attrs["session.id"] == "from-the-gateway"  # the trusted caller's, where the application set none
    assert "tenant.plan" not in attrs

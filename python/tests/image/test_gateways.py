# SPDX-License-Identifier: Apache-2.0

"""AI gateways. The calls that pass through a LiteLLM proxy or a Portkey gateway arrive at the kit's gateway
collector as spans with content, attributed to the gateway, once the one setting is made.

The setting is gateways/settings.json: the text Cover your AI shows (the impact.ai service vendors that file), with
{gateway}, {namespace} and {name} where it puts the kit's collector and the gateway's workload. The tests render it with
the mock as the collector and run the real gateway images, pinned by digest, against the mock as the model provider.

  LiteLLM  `callbacks: ["otel"]` and four variables. It adds /v1/traces to OTEL_ENDPOINT itself, and puts
           OTEL_RESOURCE_ATTRIBUTES on its spans. The content is on the span only with
           OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only (the value `true` writes it as log events
           instead). Since 1.108 it refuses to start without a master key.
  Portkey  the open-source image carries no OpenTelemetry export at all; the environment block is the one Portkey
           documents for its Enterprise and self-hosted edition, whose image is not public: the row for a Portkey
           gateway says so.
"""

from __future__ import annotations

import gzip
import json
import time
import uuid
from pathlib import Path

import pytest

from containers import Container, Mock, http, image_exists, run, wait_until

# v1.108.0 and 1.15.2 (the open-source gateway, the latest release), by the digest of the multi-architecture index
LITELLM = "docker.io/litellm/litellm@sha256:c560d317136880ea92d62334d4367ab10955b1af189dd3ea24f6551290b1d6c1"
PORTKEY = "docker.io/portkeyai/gateway@sha256:97f094d9c8a764cbfaa2a7138c0017b247ca923bb06db1b4c13b7f8a33b5200d"
SETTINGS = json.loads((Path(__file__).parent / "gateways" / "settings.json").read_text())
PROMPT, ANSWER = "hi there", "Hello from the stub."
NAMESPACE, WORKLOAD = "ai-gateway", "litellm"


def rendered(name: str, *, gateway: str, workload: str = WORKLOAD) -> tuple[str | None, dict[str, str]]:
    """The gateway's setting as Cover your AI shows it: its configuration fragment and its environment variables."""
    entry = SETTINGS["gateways"][name]
    values = {"gateway": gateway, "namespace": NAMESPACE, "name": workload}
    env = {key: value.format(**values) for key, value in entry["env"].items()}
    return entry["configYaml"], env


def ensure(reference: str) -> str:
    if not image_exists(reference):
        run("pull", "-q", reference, timeout=1800)
    return reference


def anyvalue(value):
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [anyvalue(v) for v in value.array_value.values]
    return getattr(value, kind) if kind else None


def otlp_spans(mock: Mock, path: str = "/v1/traces") -> list[dict]:
    """Every span the gateway exported (OTLP/HTTP protobuf, what both gateways' exporters send), with typed
    attributes."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    out = []
    for record in mock.requests(path):
        raw = Mock.body(record)
        if record["headers"].get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        request = ExportTraceServiceRequest()
        request.ParseFromString(raw)
        for resource_spans in request.resource_spans:
            resource = {a.key: anyvalue(a.value) for a in resource_spans.resource.attributes}
            for scope_spans in resource_spans.scope_spans:
                for span in scope_spans.spans:
                    out.append(
                        {
                            "name": span.name,
                            "kind": span.kind,
                            "scope": scope_spans.scope.name,
                            "attrs": {a.key: anyvalue(a.value) for a in span.attributes},
                            "resource": resource,
                            "span_id": span.span_id.hex(),
                            "parent": span.parent_span_id.hex(),
                        }
                    )
    return out


def model_spans(mock: Mock) -> list[dict]:
    return [s for s in otlp_spans(mock) if "gen_ai.request.model" in s["attrs"]]


def wait_for_gateway(container: Container, path: str, *, timeout: float = 180) -> None:
    """Until the gateway answers on `path`; at once, with its log, if it has stopped instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not container.is_running():
            raise AssertionError(f"the gateway stopped\n{container.logs()[-3000:]}")
        try:
            if http("GET", f"{container.url}{path}")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(1.0)
    raise AssertionError(f"the gateway did not answer on {path} in {timeout:.0f} s\n{container.logs()[-3000:]}")


def chat(gateway: Container, key: str = "sk-test", **headers) -> tuple[int, object]:
    body = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": PROMPT}],
    }
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310
        f"{gateway.url}/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", **headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


@pytest.fixture
def litellm(network, mock, tmp_path):
    """Start the pinned LiteLLM proxy with the setting as Cover your AI shows it, routing gpt-4o-mini to the mock."""
    started: list[Container] = []

    def start(env: dict[str, str] | None = None) -> Container:
        ensure(LITELLM)
        fragment, variables = rendered("litellm", gateway=mock.internal_url)
        config = (
            "model_list:\n"
            "  - model_name: gpt-4o-mini\n"
            "    litellm_params:\n"
            "      model: openai/gpt-4o-mini\n"
            f"      api_base: {mock.internal_url}/v1\n"
            "      api_key: sk-test\n" + fragment
        )
        path = tmp_path / "config.yaml"
        path.write_text(config)
        path.chmod(0o644)
        # Since 1.108 the proxy refuses to start without a master key of its own (it would accept every request).
        key = f"sk-{uuid.uuid4().hex}"
        container = Container.start(
            LITELLM,
            network=network,
            command=["--config", "/app/config.yaml", "--port", "4000"],
            env={**variables, **(env or {}), "LITELLM_LOG": "ERROR", "LITELLM_MASTER_KEY": key},
            volumes={str(path): "/app/config.yaml:ro"},
            publish=4000,
        )
        container.extra["key"] = key
        started.append(container)
        wait_for_gateway(container, "/health/liveliness")
        return container

    yield start
    for container in started:
        container.stop()


@pytest.mark.gateways
def test_the_settings_text_names_the_images_it_is_for():
    assert any(word in LITELLM for word in SETTINGS["gateways"]["litellm"]["imageNames"])
    assert any(word in PORTKEY for word in SETTINGS["gateways"]["portkey"]["imageNames"])
    for entry in SETTINGS["gateways"].values():
        assert all("{" not in word for word in entry["imageNames"])
        for value in entry["env"].values():
            assert not [name for name in ("{gateway}", "{namespace}", "{name}") if value.count(name) > 1]


@pytest.mark.gateways
def test_litellm_with_the_one_setting_sends_its_calls_with_content_attributed_to_the_gateway(litellm, mock):
    gateway = litellm()
    status, body = chat(gateway, gateway.extra["key"])
    assert status == 200, gateway.logs()[-2000:]
    assert body["choices"][0]["message"]["content"] == ANSWER
    assert mock.requests("/v1/chat/completions"), "the call went through the gateway to the provider"

    wait_until(
        lambda: model_spans(mock),
        timeout=60,
        message=lambda: (
            f"a model span at the collector; got {[(s['name']) for s in otlp_spans(mock)]}\n{gateway.logs()[-2000:]}"
        ),
    )
    spans = [s for s in model_spans(mock) if s["attrs"]["gen_ai.request.model"] == "gpt-4o-mini"]
    # LiteLLM writes the model call on the spans of its own (the request, the call to the provider): the content is on
    # at least one, with the model, the tokens and the messages as the GenAI conventions have them
    carrying = [s for s in spans if "gen_ai.input.messages" in s["attrs"]]
    assert carrying, [s["attrs"] for s in spans]
    for span in carrying:
        attrs = span["attrs"]
        assert PROMPT in attrs["gen_ai.input.messages"], attrs
        assert ANSWER in attrs["gen_ai.output.messages"], attrs
    assert any(
        s["attrs"].get("gen_ai.usage.input_tokens") == 7 and s["attrs"].get("gen_ai.usage.output_tokens") == 5
        for s in spans
    )
    # attributed to the gateway: the placement of the gateway's own workload, from OTEL_RESOURCE_ATTRIBUTES, on every
    # span
    for span in otlp_spans(mock):
        assert span["resource"]["k8s.namespace.name"] == NAMESPACE, span["resource"]
        assert span["resource"]["k8s.deployment.name"] == WORKLOAD, span["resource"]
    # no log events: the content is on the span
    assert mock.requests("/v1/logs") == []


@pytest.mark.gateways
def test_litellm_with_the_value_true_keeps_the_content_off_the_span(litellm, mock):
    """Why the setting says span_only: `true` is the log-event mode, and the span carries no content."""
    gateway = litellm({"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true"})
    assert chat(gateway, gateway.extra["key"])[0] == 200
    wait_until(lambda: model_spans(mock), timeout=60, message=lambda: f"a model span\n{gateway.logs()[-2000:]}")
    for span in model_spans(mock):
        assert "gen_ai.input.messages" not in span["attrs"] and "gen_ai.output.messages" not in span["attrs"], span[
            "attrs"
        ]


@pytest.mark.gateways
def test_the_open_source_portkey_gateway_exports_nothing_with_the_documented_environment_block(network, mock):
    """The block of Portkey's documentation (Enterprise and self-hosted editions) does nothing in the open-source image.
    The row for a Portkey gateway therefore says that its spans need the Enterprise or self-hosted edition."""
    ensure(PORTKEY)
    _, env = rendered("portkey", gateway=mock.internal_url, workload="portkey")
    gateway = Container.start(PORTKEY, network=network, env=env, publish=8787)
    try:
        wait_for_gateway(gateway, "/", timeout=120)
        status, body = chat(
            gateway, **{"x-portkey-provider": "openai", "x-portkey-custom-host": f"{mock.internal_url}/v1"}
        )
        assert status == 200, (body, gateway.logs()[-2000:])
        assert body["choices"][0]["message"]["content"] == ANSWER
        assert mock.requests("/v1/chat/completions"), "the call went through the gateway to the provider"
        time.sleep(10)  # longer than any batch delay
        for path in ("/v1/traces", "/v1/otel", "/v1/logs", "/v1/otel/v1/traces"):
            assert mock.requests(path) == [], path
    finally:
        gateway.stop()

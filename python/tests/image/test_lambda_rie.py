# SPDX-License-Identifier: Apache-2.0

"""The Python layer in AWS's own Lambda images, under the Runtime Interface Emulator they carry (make test-lambda).

Each cell runs a function that calls Amazon Bedrock's Converse API (the mock's) through the boto3 the image ships, with
the layer's archive (make lambda-layers) unpacked into /opt as Lambda unpacks it and only the settings a function's
change adds: AWS_LAMBDA_EXEC_WRAPPER and the OTLP export settings. The mock is the ingest too. The cells use the
architecture of the machine that runs them; the archive of the other is built and checked by make lambda-layers.
"""

from __future__ import annotations

import json
import os
import platform
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest

from containers import Container, wait_until

pytestmark = pytest.mark.lambda_rie

ROOT = Path(__file__).resolve().parents[3]
LAYERS = Path(os.environ.get("LAMBDA_LAYERS_DIR", ROOT / "build" / "lambda"))
ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
# AWS's Lambda base images for Python, by the digest of their multi-architecture index.
LAMBDA_IMAGES = json.loads((ROOT / "lambda" / "rie-images.json").read_text())["images"]
KEY = "lambda-test-key"
MODEL, SPAN_MODEL = (
    "anthropic.claude-3-haiku-20240307-v1:0",
    "claude-3-haiku-20240307-v1:0",
)  # the span drops the vendor

APP = """
import os

import boto3

client = boto3.client(
    "bedrock-runtime", endpoint_url=os.environ["BEDROCK_ENDPOINT"], region_name=os.environ["AWS_REGION"]
)


def handler(event, context):
    response = client.converse(
        modelId=os.environ["MODEL_ID"],
        messages=[{"role": "user", "content": [{"text": event.get("prompt", "Refund order A-1")}]}],
    )
    return {"answer": response["output"]["message"]["content"][0]["text"], "order": event.get("order")}
"""

DATADOG_HANDLER = """
import importlib
import os

_module, _, _name = os.environ["DD_LAMBDA_HANDLER"].rpartition(".")
handler = getattr(importlib.import_module(_module), _name)
"""


@pytest.fixture(scope="session")
def layer_dir(tmp_path_factory):
    """The archive unpacked as Lambda unpacks a layer into /opt, the modes of its entries kept."""
    archive = LAYERS / f"impact-ai-python-{ARCH}.zip"
    if not archive.exists():
        pytest.fail(f"no {archive}: run `make lambda-layers` first", pytrace=False)
    opt = tmp_path_factory.mktemp("opt")
    with zipfile.ZipFile(archive) as layer:
        for info in layer.infolist():
            path = Path(layer.extract(info, opt))
            path.chmod((info.external_attr >> 16) & 0o777 or 0o644)
    for path in [opt, *opt.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
    return opt


@pytest.fixture
def code_dir(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    vendor = tmp_path / "datadog_lambda"
    vendor.mkdir()
    (vendor / "__init__.py").write_text("")
    (vendor / "handler.py").write_text(DATADOG_HANDLER)
    for path in [tmp_path, *tmp_path.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return tmp_path


class Function:
    """A function in a Lambda image, invoked through the emulator."""

    def __init__(self, container: Container):
        self.container = container

    def invoke(self, event: dict, timeout: float = 30) -> tuple[dict | str, float]:
        url = f"{self.container.url}/2015-03-31/functions/function/invocations"
        request = urllib.request.Request(url, data=json.dumps(event).encode(), method="POST")  # noqa: S310
        begin = time.monotonic()
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the local emulator
            body = response.read().decode()
        try:
            return json.loads(body), time.monotonic() - begin
        except ValueError:
            return body, time.monotonic() - begin


def start_function(python, *, network, mock, layer_dir, code_dir, handler="app.handler", layer=True, **env) -> Function:
    settings = {
        "AWS_LAMBDA_FUNCTION_NAME": "refund-bot",
        "AWS_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "fake-access-key",
        "AWS_SECRET_ACCESS_KEY": "fake-secret-key",
        "BEDROCK_ENDPOINT": mock.internal_url,
        "MODEL_ID": MODEL,
        "OTEL_EXPORTER_OTLP_ENDPOINT": mock.internal_url,
        "OTEL_EXPORTER_OTLP_HEADERS": f"authorization=Bearer%20{KEY}",
    }
    if layer:
        settings["AWS_LAMBDA_EXEC_WRAPPER"] = "/opt/impact-ai-instrument"
    settings.update({k: v for k, v in env.items() if v is not None})
    for name in [k for k, v in env.items() if v is None]:
        settings.pop(name, None)
    volumes = {str(code_dir): "/var/task:ro"}
    if layer:
        volumes[str(layer_dir)] = "/opt:ro"
    container = Container.start(
        LAMBDA_IMAGES[python],
        network=network,
        command=[handler],
        env=settings,
        volumes=volumes,
        publish=8080,
        name=f"i019-lambda-{uuid.uuid4().hex[:8]}",
    )
    function = Function(container)

    def ready():
        try:
            urllib.request.urlopen(f"{container.url}/", timeout=1)  # noqa: S310
        except urllib.error.HTTPError:
            return True  # the emulator answers: 404 on /
        except OSError:
            return False
        return True

    wait_until(ready, timeout=30, message=lambda: f"the emulator to listen:\n{container.logs()}")
    return function


def spans_of(mock) -> list[dict]:
    return [s for s in mock.spans() if s["name"] != "impact.setup"]


def setup_reports(mock) -> list[dict]:
    return [s for s in mock.spans() if s["name"] == "impact.setup"]


@pytest.fixture
def function_factory(network, mock, layer_dir, code_dir):
    started: list[Function] = []

    def make(python="3.12", **kwargs) -> Function:
        function = start_function(python, network=network, mock=mock, layer_dir=layer_dir, code_dir=code_dir, **kwargs)
        started.append(function)
        return function

    yield make
    for function in started:
        function.container.stop()


# --- One change covers a function ------------------------------------------------------------------------------------


@pytest.mark.parametrize("python", sorted(LAMBDA_IMAGES))
def test_the_layer_traces_a_functions_bedrock_calls(python, function_factory, mock):
    mock.control(require_key=KEY)
    function = function_factory(python)
    for number in range(3):
        result, _ = function.invoke({"order": f"A-{number}"})
        assert result["order"] == f"A-{number}", function.container.logs()
        assert result["answer"]
    # Each invocation sends its spans before it returns (or with the next one): no wait for a schedule here.
    wait_until(
        lambda: len([s for s in spans_of(mock) if s["attrs"].get("gen_ai.request.model")]) >= 3,
        timeout=10,
        message=lambda: f"three model spans; got {[s['name'] for s in mock.spans()]}\n{function.container.logs()}",
    )
    calls = [s for s in spans_of(mock) if s["attrs"].get("gen_ai.request.model")]
    for span in calls:
        assert span["attrs"]["gen_ai.request.model"] == SPAN_MODEL
        assert span["attrs"]["gen_ai.provider.name"] == "aws.bedrock"
        assert span["attrs"].get("gen_ai.usage.input_tokens") == 7
        resource = span["resource"]
        assert resource["faas.name"] == "refund-bot"
        assert resource["service.name"] == "refund-bot"
        assert resource["cloud.provider"] == "aws"
        assert resource["cloud.platform"] == "aws_lambda"
        assert resource["cloud.region"] == "eu-west-1"
        assert resource["cloud.resource_id"].startswith("arn:aws:lambda:")
        assert resource["cloud.resource_id"].endswith(":function:refund-bot")
        assert len(resource["cloud.account.id"]) == 12 and resource["cloud.account.id"].isdigit()
    reports = setup_reports(mock)
    assert len(reports) == 1
    assert reports[0]["attrs"]["impact.setup.state"] == "active"
    assert reports[0]["attrs"]["impact.setup.python.version"].startswith(python + ".")
    assert reports[0]["resource"]["faas.name"] == "refund-bot"
    traces = mock.requests("/v1/traces")
    assert traces
    assert all({k.lower(): v for k, v in r["headers"].items()}["authorization"] == f"Bearer {KEY}" for r in traces)


def test_the_function_runs_as_without_the_layer(function_factory, mock):
    plain = function_factory("3.12", layer=False)
    with_layer = function_factory("3.12")
    for number in range(2):
        assert plain.invoke({"order": f"A-{number}"})[0] == with_layer.invoke({"order": f"A-{number}"})[0]


# --- The function always runs ----------------------------------------------------------------------------------------


def test_an_ingest_that_never_answers_costs_an_invocation_at_most_the_flush_bound(function_factory, mock):
    mock.control(rules=[{"path": "/v1/traces", "delay": 60}])
    function = function_factory("3.12", IMPACT_LAMBDA_FLUSH_MS="100")
    function.invoke({"order": "warm"})  # the cold start: init is not an invocation's time
    for number in range(3):
        result, seconds = function.invoke({"order": f"A-{number}"})
        assert result["order"] == f"A-{number}"
        assert seconds < 1.0, function.container.logs()


# The proposed budget for what the layer adds to an invocation at the 95th percentile (scripts/lambda_timings.py).
INVOCATION_P95_BUDGET_SECONDS = 0.025


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * 0.95
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def test_an_ingest_that_hangs_costs_the_invocations_after_the_first_no_more_than_the_budget(function_factory, mock):
    # Accepts the connection and does not answer for a minute: a blackholed ingest, at the default flush bound.
    mock.control(rules=[{"path": "/v1/traces", "delay": 60}])
    plain = function_factory("3.12", layer=False)
    with_layer = function_factory("3.12")
    for function in (plain, with_layer):
        function.invoke({"order": "cold"})  # init, and the one wait that runs out
        function.invoke({"order": "warm"})
    times: dict[str, list[float]] = {"plain": [], "layer": []}
    for number in range(20):  # interleaved, so that both sides share the machine's noise
        for side, function in (("plain", plain), ("layer", with_layer)):
            result, seconds = function.invoke({"order": f"A-{number}"})
            assert result["order"] == f"A-{number}", function.container.logs()
            times[side].append(seconds)
    added = p95(times["layer"]) - p95(times["plain"])
    milliseconds = {side: [round(t * 1000, 1) for t in values] for side, values in times.items()}
    assert added <= INVOCATION_P95_BUDGET_SECONDS, (
        f"+{added * 1000:.1f} ms at p95 while ingest hangs: {milliseconds}\n{with_layer.container.logs()}"
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20revoked-key"},  # the key was revoked: 401
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://ingest.invalid:4318"},  # no route to the ingest host
        {"OTEL_EXPORTER_OTLP_ENDPOINT": None, "OTEL_EXPORTER_OTLP_HEADERS": None},  # no export settings at all
    ],
    ids=["key-revoked", "ingest-unreachable", "no-settings"],
)
def test_the_function_runs_whatever_ingest_does(settings, function_factory, mock):
    mock.control(require_key=KEY)
    function = function_factory("3.12", **settings)
    for number in range(2):
        result, seconds = function.invoke({"order": f"A-{number}"})
        assert result["order"] == f"A-{number}", function.container.logs()
        assert seconds < 5.0


# --- One wrapper -----------------------------------------------------------------------------------------------------


def test_beside_another_tracers_lambda_handler_the_layer_stands_down(function_factory, mock):
    function = function_factory("3.12", handler="datadog_lambda.handler.handler", DD_LAMBDA_HANDLER="app.handler")
    result, _ = function.invoke({"order": "A-1"})
    assert result["order"] == "A-1"
    time.sleep(1.0)
    assert mock.requests("/v1/traces") == []


def test_the_spans_attributes_are_names_never_the_environment(function_factory, mock):
    function = function_factory("3.12", REFUND_API_TOKEN="planted-value-never-sent")  # noqa: S106 - a planted fake
    function.invoke({"order": "A-1"})
    wait_until(lambda: setup_reports(mock), timeout=10)
    for record in mock.requests("/v1/traces"):
        assert "planted-value-never-sent" not in json.dumps(record.get("json"))

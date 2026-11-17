# SPDX-License-Identifier: Apache-2.0

"""The kit's gateway forwards log events beside spans.

Instrumentations that follow the OpenTelemetry GenAI conventions write a model call's prompt and answer as log events
(Java, .NET and some Node.js instrumentations, Python's botocore). The tests run the Collector image the chart
deploys, pinned by digest, with the configuration the chart renders, against the mock ingest, and send it what such an
instrumentation sends: OTLP/HTTP log records with a GenAI event as the body, joined to their span by trace ID and span
ID.

What they pin: the records reach `/v1/logs` with the cluster's attributes beside the workload's own, under the key, gzip
and retries that spans get; spans still reach `/v1/traces` with the same attributes; and the memory ceiling stays one
setting, with a queue of 2,000 batches for each signal (the figure the chart README states).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from containers import Container, Mock, http, image_exists, protobuf_logs, protobuf_spans, run, wait_until

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "chart" / "impact-ai"
COLLECTOR = yaml.safe_load((CHART / "values.yaml").read_text())["images"]["collector"]
IMAGE = f"{COLLECTOR['repository']}@{COLLECTOR['digest']}"

KEY = "kit-gateway-logs-key-5d2b7f"  # the ingest key the gateway holds
UID = "4e5622e7-f420-499c-9662-50af7f8a64b9"
ARN = "arn:aws:eks:eu-west-1:012345678901:cluster/prod-eks"
# A cluster name is free text from the install command: this one holds a reference to the gateway's key, which has to
# reach the log records as the text it is (the escape itself is tested in chart/tests/test_chart_gateway_literals.py).
CLUSTER_NAME = "prod ${env:IMPACT_INGEST_KEY} $"
CLUSTER = {
    "k8s.cluster.uid": UID,
    "k8s.cluster.name": CLUSTER_NAME,
    "cloud.provider": "aws",
    "cloud.account.id": "012345678901",
    "cloud.region": "eu-west-1",
    "cloud.resource_id": ARN,
}

TRACE_ID, SPAN_ID = "5b8efff798038103d269b633813fc60c", "eee19b7ec3c1b174"
PROMPT, ANSWER = "Where is my order?", "It ships tomorrow."


def attribute(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


# What the workload's own instrumentation puts on the resource: its name, a region it detected itself (which the gateway
# leaves as it is) and a cluster name of its own (which the gateway replaces with the install's).
WORKLOAD = [
    attribute("service.name", "returns-java"),
    attribute("cloud.region", "eu-central-1"),
    attribute("k8s.cluster.name", "named-by-the-workload"),
]
AS_FORWARDED = {**CLUSTER, "service.name": "returns-java", "cloud.region": "eu-central-1"}


def log_events() -> dict:
    """An OTLP/JSON export of two GenAI events of one model call: the user's message, named by the OTLP `eventName`
    field, and the choice, named by the `event.name` attribute (the Java agent's form)."""
    user = {
        "timeUnixNano": "1762326000000000000",
        "observedTimeUnixNano": "1762326000000000100",
        "severityNumber": 9,
        "severityText": "INFO",
        "eventName": "gen_ai.user.message",
        "body": {"kvlistValue": {"values": [attribute("content", PROMPT), attribute("role", "user")]}},
        "attributes": [attribute("gen_ai.system", "aws.bedrock")],
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
    }
    message = {"kvlistValue": {"values": [attribute("content", ANSWER), attribute("role", "assistant")]}}
    choice = {
        "timeUnixNano": "1762326001000000000",
        "severityNumber": 9,
        "severityText": "INFO",
        "attributes": [attribute("event.name", "gen_ai.choice"), attribute("gen_ai.system", "aws.bedrock")],
        "body": {
            "kvlistValue": {
                "values": [
                    {"key": "index", "value": {"intValue": "0"}},
                    attribute("finish_reason", "stop"),
                    {"key": "message", "value": message},
                ]
            }
        },
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
    }
    scope = {"name": "io.opentelemetry.aws-sdk"}
    return {
        "resourceLogs": [
            {"resource": {"attributes": WORKLOAD}, "scopeLogs": [{"scope": scope, "logRecords": [user, choice]}]}
        ]
    }


def a_span() -> dict:
    """An OTLP/JSON export of the span of that call, from the same workload."""
    span = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "name": "chat nova-micro-v1:0",
        "kind": 3,
        "startTimeUnixNano": "1762325999000000000",
        "endTimeUnixNano": "1762326002000000000",
        "attributes": [attribute("gen_ai.request.model", "nova-micro-v1:0")],
    }
    return {
        "resourceSpans": [
            {"resource": {"attributes": WORKLOAD}, "scopeSpans": [{"scope": {"name": "x"}, "spans": [span]}]}
        ]
    }


def ensure(reference: str) -> str:
    if not image_exists(reference):
        run("pull", "-q", reference, timeout=1800)
    return reference


def rendered_config() -> str:
    """The gateway's config.yaml as the chart renders it for a cluster with a name, an ARN and a cloud identity."""
    with tempfile.TemporaryDirectory(prefix="kit-gateway-render-") as directory:
        values = Path(directory) / "values.yaml"
        values.write_text(yaml.safe_dump({"cluster": {"name": CLUSTER_NAME, "arn": ARN}}))
        rendered = subprocess.run(  # noqa: S603 - helm with test-controlled arguments
            [
                *("helm", "template", "impact-ai", str(CHART), "-n", "impact-ai"),
                *("-f", str(CHART / "ci" / "values.yaml"), "-f", str(values)),
                *("--set", "cloud.provider=aws", "--set-string", "cloud.accountId=012345678901"),
                *("--set", "cloud.region=eu-west-1", "--show-only", "templates/gateway-config.yaml"),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    return yaml.safe_load(rendered)["data"]["config.yaml"]


class Gateway:
    """The pinned Collector, started with a configuration, on the network of the mock it forwards to."""

    def __init__(self, container: Container, directory: Path):
        self.container = container
        self.directory = directory

    def post(self, path: str, body: dict) -> int:
        status, _ = http("POST", f"{self.container.url}{path}", body)
        return status

    def metrics(self) -> str:
        url = f"http://127.0.0.1:{self.container.published(8888)}/metrics"
        return urllib.request.urlopen(url, timeout=10).read().decode()  # noqa: S310


@pytest.fixture
def gateway(network, mock):
    """`start()` runs a gateway on the chart's configuration, or on the one given; the fixture removes what it ran."""
    ensure(IMAGE)
    started: list[Gateway] = []

    def start(config: str | None = None, *, metrics: bool = False) -> Gateway:
        # not under pytest's private directory: the image's user reads it
        directory = Path(tempfile.mkdtemp(prefix="kit-gateway-logs-"))
        directory.chmod(0o755)
        (directory / "config.yaml").write_text(config if config is not None else rendered_config())
        (directory / "config.yaml").chmod(0o644)
        container = Container.start(
            IMAGE,
            network=network,
            command=["--config=/conf/config.yaml"],
            env={"IMPACT_INGEST_ENDPOINT": mock.internal_url, "IMPACT_INGEST_KEY": KEY},
            volumes={str(directory): "/conf:ro"},
            publish=4318,
            extra_args=["-p", "127.0.0.1::8888"] if metrics else None,
        )
        found = Gateway(container, directory)
        started.append(found)
        # the receiver answers a GET with 405 once it listens (on the route every configuration has, the spans'); the
        # container's log says why, if it stopped instead
        wait_until(
            lambda: http("GET", f"{container.url}/v1/traces")[0] == 405,
            timeout=60,
            message=lambda: f"the gateway to listen\n{container.logs()[-3000:]}",
        )
        return found

    yield start
    for found in started:
        found.container.stop()
        shutil.rmtree(found.directory, ignore_errors=True)


def accepted(mock: Mock, path: str = "/v1/logs") -> list[dict]:
    """The requests ingest took (answered 200) on `path`."""
    return [r for r in mock.requests(path) if r.get("status") == 200]


@pytest.mark.image
def test_log_events_with_genai_content_reach_ingest_with_the_clusters_attributes(gateway, mock):
    mock.control(require_key=KEY)
    started = gateway()
    assert started.post("/v1/logs", log_events()) == 200

    wait_until(lambda: accepted(mock), timeout=30, message="the log events at ingest")
    (request,) = accepted(mock)
    assert request["method"] == "POST" and request["path"] == "/v1/logs"
    assert request["headers"]["Authorization"] == f"Bearer {KEY}"  # the key spans are sent with
    assert request["headers"]["Content-Encoding"] == "gzip"
    assert request["headers"]["Content-Type"] == "application/x-protobuf"

    user, choice = protobuf_logs(mock)
    # the events as the instrumentation wrote them: name, body, attributes, time and the ids that join them to a span
    assert user["event_name"] == "gen_ai.user.message"
    assert user["body"] == {"content": PROMPT, "role": "user"}
    assert user["attrs"] == {"gen_ai.system": "aws.bedrock"}
    assert choice["event_name"] == "" and choice["attrs"]["event.name"] == "gen_ai.choice"
    assert choice["body"] == {"index": 0, "finish_reason": "stop", "message": {"content": ANSWER, "role": "assistant"}}
    for event in (user, choice):
        assert (event["trace_id"], event["span_id"]) == (TRACE_ID, SPAN_ID)
        assert event["scope"] == "io.opentelemetry.aws-sdk" and event["severity"] == "INFO"
    assert (user["time"], choice["time"]) == (1762326000000000000, 1762326001000000000)

    # the placement: the cluster's attributes beside the workload's own. The install's cluster name replaces a
    # workload's, a region the workload detected itself stays, and the reference in the name reaches ingest as the
    # text it is.
    assert user["resource"] == choice["resource"] == AS_FORWARDED
    assert KEY not in json.dumps(user["resource"])
    assert mock.requests("/v1/traces") == [], "log events are not sent as spans"


@pytest.mark.image
def test_spans_still_reach_traces_and_carry_the_placement_the_log_events_carry(gateway, mock):
    mock.control(require_key=KEY)
    started = gateway()
    assert started.post("/v1/traces", a_span()) == 200
    assert started.post("/v1/logs", log_events()) == 200

    wait_until(lambda: accepted(mock, "/v1/traces") and accepted(mock), timeout=30, message="a span and its log events")
    for path in ("/v1/traces", "/v1/logs"):
        (request,) = accepted(mock, path)
        assert (
            request["headers"]["Authorization"] == f"Bearer {KEY}" and request["headers"]["Content-Encoding"] == "gzip"
        )
    (span,) = protobuf_spans(mock)
    assert span["name"] == "chat nova-micro-v1:0" and span["attrs"] == {"gen_ai.request.model": "nova-micro-v1:0"}
    assert span["resource"] == AS_FORWARDED, "a span and a log event of one workload carry the same placement"
    assert all(event["resource"] == span["resource"] for event in protobuf_logs(mock))


@pytest.mark.image
def test_a_key_ingest_refuses_is_refused_for_log_events_as_for_spans(gateway, mock):
    mock.control(require_key="a-key-the-gateway-does-not-hold")
    started = gateway()
    assert started.post("/v1/traces", a_span()) == 200 and started.post("/v1/logs", log_events()) == 200

    def refused():
        return all(any(r.get("status") == 401 for r in mock.requests(path)) for path in ("/v1/traces", "/v1/logs"))

    wait_until(refused, timeout=30, message="401 receipts for both routes")
    for path in ("/v1/traces", "/v1/logs"):
        assert {r["headers"]["Authorization"] for r in mock.requests(path)} == {f"Bearer {KEY}"}
        assert not accepted(mock, path)
    assert started.container.is_running()


@pytest.mark.image
def test_a_log_batch_that_ingest_refuses_for_now_is_tried_again_as_a_span_batch_is(gateway, mock):
    mock.control(rules=[{"path": "/v1/logs", "status": 503, "retry_after": 1, "times": 2}], require_key=KEY)
    started = gateway()
    assert started.post("/v1/logs", log_events()) == 200

    wait_until(lambda: accepted(mock), timeout=60, message="the log events, after two refusals")
    assert [r["status"] for r in mock.requests("/v1/logs")] == [503, 503, 200]
    # ingest took the two events once, in order: those of the request it answered 200 are the two that were sent
    user, choice = (event for event in protobuf_logs(mock) if event["status"] == 200)
    assert user["event_name"] == "gen_ai.user.message" and user["body"]["content"] == PROMPT
    assert choice["attrs"]["event.name"] == "gen_ai.choice" and choice["body"]["message"]["content"] == ANSWER
    assert started.container.is_running()


@pytest.mark.image
def test_ingest_without_the_logs_route_costs_the_log_events_only(gateway, mock):
    # An ingest that has not been given the route answers 404: not a failure to try again. The batch is dropped, the
    # spans go on, and the gateway forwards log events again as soon as the route is there.
    mock.control(rules=[{"path": "/v1/logs", "status": 404}], require_key=KEY)
    started = gateway()
    assert started.post("/v1/logs", log_events()) == 200 and started.post("/v1/traces", a_span()) == 200

    wait_until(lambda: accepted(mock, "/v1/traces"), timeout=30, message="the span at ingest")
    wait_until(lambda: mock.requests("/v1/logs"), timeout=30, message="the log events tried")
    time.sleep(4)  # longer than the first retry interval, if there were to be one
    assert [r["status"] for r in mock.requests("/v1/logs")] == [404]
    assert started.container.is_running()

    mock.control(require_key=KEY)  # the route is there now
    assert started.post("/v1/logs", log_events()) == 200
    wait_until(lambda: accepted(mock), timeout=30, message="the log events, once ingest has the route")
    assert started.container.is_running()


@pytest.mark.image
def test_the_collector_makes_a_queue_of_the_configured_size_for_each_signal_and_one_memory_limiter(gateway, mock):
    # The chart README says: spans and log events each in a queue in memory (2,000 batches each). The Collector's own
    # metrics, switched on in a copy of the chart's configuration (the chart's has them off), say how many queues there
    # are and how big.
    config = yaml.safe_load(rendered_config())
    config["service"]["telemetry"]["metrics"] = {
        "level": "detailed",
        "readers": [{"pull": {"exporter": {"prometheus": {"host": "0.0.0.0", "port": 8888}}}}],  # noqa: S104
    }
    mock.control(require_key=KEY)
    started = gateway(yaml.safe_dump(config), metrics=True)

    def capacities() -> dict[str, float]:
        found = {}
        for line in started.metrics().splitlines():
            if line.startswith("otelcol_exporter_queue_capacity{"):
                found[line.split('data_type="', 1)[1].split('"', 1)[0]] = float(line.rsplit(" ", 1)[1])
        return found

    assert wait_until(capacities, timeout=30, message="the queue metrics") == {"logs": 2000.0, "traces": 2000.0}
    # one limiter serves both pipelines (the Collector starts one instance for equal settings), so the ceiling is one
    limiter = [line for line in started.container.logs().splitlines() if "Memory limiter configured" in line]
    assert len(limiter) == 1, limiter

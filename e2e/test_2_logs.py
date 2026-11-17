# SPDX-License-Identifier: Apache-2.0

"""A log event a workload sends to the kit's gateway reaches impact.ai on /v1/logs, with the cluster's attributes.

The kit's own packages send spans. A workload whose own OpenTelemetry instrumentation writes a model call's prompt and
answer as log events (Java, .NET and some Node.js instrumentations, Python's botocore) is pointed at the gateway
with OTEL_EXPORTER_OTLP_LOGS_ENDPOINT. The test sends such an event from inside a pod of the cluster, to the gateway's
Service, as an SDK does (OTLP/HTTP, protobuf), and reads it at the mock ingest: through the chart's own configuration,
Service and key.
"""

from __future__ import annotations

import base64

from kube import CLOUD_ON_SPANS, CLUSTER_NAME, KEY, kubectl, wait_until

# What the README gives a workload to set: the gateway's Service in the release namespace, port 4318, the logs route.
GATEWAY_LOGS = "http://impact-ai-gateway.impact-ai.svc:4318/v1/logs"
TRACE_ID, SPAN_ID = "5b8efff798038103d269b633813fc60c", "eee19b7ec3c1b174"
PROMPT, ANSWER = "Where is my order?", "It ships tomorrow."


def export_request() -> bytes:
    """An ExportLogsServiceRequest of the two GenAI events of one model call, the way an SDK serializes it: the user's
    message, named by the OTLP `event_name` field, and the choice."""
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue, KeyValueList
    from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource

    def text(key: str, value: str) -> KeyValue:
        return KeyValue(key=key, value=AnyValue(string_value=value))

    def fields(*items: KeyValue) -> AnyValue:
        return AnyValue(kvlist_value=KeyValueList(values=list(items)))

    user = LogRecord(
        time_unix_nano=1762326000000000000,
        severity_number=9,
        severity_text="INFO",
        event_name="gen_ai.user.message",
        body=fields(text("content", PROMPT), text("role", "user")),
        attributes=[text("gen_ai.system", "aws.bedrock")],
        trace_id=bytes.fromhex(TRACE_ID),
        span_id=bytes.fromhex(SPAN_ID),
    )
    choice = LogRecord(
        time_unix_nano=1762326001000000000,
        severity_number=9,
        severity_text="INFO",
        event_name="gen_ai.choice",
        body=fields(text("finish_reason", "stop"), KeyValue(key="message", value=fields(text("content", ANSWER)))),
        attributes=[text("gen_ai.system", "aws.bedrock")],
        trace_id=bytes.fromhex(TRACE_ID),
        span_id=bytes.fromhex(SPAN_ID),
    )
    resource = Resource(attributes=[text("service.name", "returns-assistant")])
    scope = ScopeLogs(log_records=[user, choice])
    return ExportLogsServiceRequest(
        resource_logs=[ResourceLogs(resource=resource, scope_logs=[scope])]
    ).SerializeToString()


def send(namespace: str, deployment: str, payload: bytes) -> str:
    """POST the export to the gateway's Service from inside a pod of the Deployment, and return the status."""
    code = (
        "import base64, urllib.request\n"
        f"data = base64.b64decode({base64.b64encode(payload).decode()!r})\n"
        f"req = urllib.request.Request({GATEWAY_LOGS!r}, data=data, "
        "headers={'Content-Type': 'application/x-protobuf'}, method='POST')\n"
        "print(urllib.request.urlopen(req, timeout=20).status)\n"
    )
    out = kubectl("-n", namespace, "exec", f"deploy/{deployment}", "-c", "app", "--", "python", "-c", code, timeout=60)
    return out.stdout.strip().splitlines()[-1]


def test_a_log_event_a_workload_sends_to_the_gateway_reaches_impact_ai_with_the_clusters_attributes(world):
    before = world.mock.last_seq()
    wait_until(
        lambda: send("shop", "support-assistant", export_request()) == "200",
        timeout=120,
        message="the gateway to take the log events",
    )

    def arrived():
        found = world.mock.logs(since=before)
        return found if len(found) >= 2 else None

    user, choice, *_ = wait_until(arrived, timeout=90, message="the two log events at impact.ai")
    # the events as the instrumentation wrote them
    assert user["event_name"] == "gen_ai.user.message" and user["body"] == {"content": PROMPT, "role": "user"}
    assert choice["event_name"] == "gen_ai.choice"
    assert choice["body"] == {"finish_reason": "stop", "message": {"content": ANSWER}}
    for event in (user, choice):
        assert (event["trace_id"], event["span_id"]) == (TRACE_ID, SPAN_ID)  # what joins them to their span
        assert event["attrs"] == {"gen_ai.system": "aws.bedrock"}
        resource = event["resource"]
        assert resource["service.name"] == "returns-assistant"  # the workload's own attributes stay
        assert resource["k8s.cluster.uid"] == world.cluster_uid  # added by the gateway, as for spans
        assert resource["k8s.cluster.name"] == CLUSTER_NAME  # the reference in it was not read as one
        assert {k: resource.get(k) for k in CLOUD_ON_SPANS} == CLOUD_ON_SPANS  # the install's cloud identity
    # under the key the cluster holds, compressed, to the route of its own
    (request, *_) = world.mock.requests("/v1/logs", before)
    assert request["status"] == 200 and request["headers"]["Authorization"] == f"Bearer {KEY}"
    assert request["headers"]["Content-Encoding"] == "gzip"
    # and no span came of it: log events are not spans
    assert not [s for s in world.mock.spans(since=before) if s["resource"].get("service.name") == "returns-assistant"]

# SPDX-License-Identifier: Apache-2.0

"""The package sends spans as OTLP/HTTP JSON using the standard library only, so that it ships no protobuf, requests,
urllib3 or certifi that could shadow the application's own copies."""

from __future__ import annotations

import base64
import json
import threading
import time

import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans as official_encode
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanContext, SpanKind, Status, StatusCode, TraceFlags

from helpers import Sink
from impact_instrumentation import exporter as impact_exporter


@pytest.fixture
def sink():
    s = Sink()
    yield s
    s.close()


def make_spans():
    """Spans that exercise every field the encoder maps."""
    memory = InMemorySpanExporter()
    resource = Resource.create({"service.name": "support-assistant", "k8s.namespace.name": "shop", "retries": 3})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer("opentelemetry.instrumentation.test", "1.2.3", schema_url="https://example/schema")
    other = provider.get_tracer("other.scope")
    linked = Link(SpanContext(0xAB, 0xCD, False, TraceFlags(1)))
    remote_parent = trace.set_span_in_context(trace.NonRecordingSpan(SpanContext(0x1234, 0x5678, True, TraceFlags(1))))
    with tracer.start_as_current_span(
        "chat gpt-4o-mini",
        kind=SpanKind.SERVER,
        attributes={
            "gen_ai.input.messages": '[{"role":"user","content":"hi there"}]',
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.request.temperature": 0.25,
            "flag": True,
            "ratios": [0.1, 0.2],
            "names": ["a", "b"],
            "ints": [1, 2, 3],
            "unicode": "héllo ☃ \U0001f600",
        },
        links=[linked],
        context=remote_parent,
    ) as span:
        span.add_event("gen_ai.choice", {"index": 0, "finish_reason": "stop"})
        span.set_status(Status(StatusCode.ERROR, "boom"))
        with tracer.start_as_current_span("child", kind=SpanKind.CLIENT):
            pass
    with other.start_as_current_span("producer", kind=SpanKind.PRODUCER):
        pass
    with other.start_as_current_span("consumer", kind=SpanKind.CONSUMER) as s:
        s.set_status(Status(StatusCode.OK))
    return memory.get_finished_spans()


def canon(value):
    """Drop protobuf default values so a protobuf JSON dump and ours compare equal."""
    if isinstance(value, dict):
        if set(value) & {
            "stringValue",
            "intValue",
            "doubleValue",
            "boolValue",
            "arrayValue",
            "kvlistValue",
            "bytesValue",
        }:
            return {k: canon(v) if k in {"arrayValue", "kvlistValue"} else v for k, v in value.items()}
        return {k: canon(v) for k, v in value.items() if canon(v) not in ("", 0, [], {}, None)}
    if isinstance(value, list):
        return [canon(v) for v in value]
    return value


def ids_to_base64(request: dict) -> dict:
    """Our JSON uses hex ids (the OTLP JSON rule); protobuf's own JSON uses base64."""

    def fix(span):
        for key in ("traceId", "spanId", "parentSpanId"):
            if key in span:
                span[key] = base64.b64encode(bytes.fromhex(span[key])).decode()
        for link in span.get("links", []):
            for key in ("traceId", "spanId"):
                link[key] = base64.b64encode(bytes.fromhex(link[key])).decode()

    for rs in request["resourceSpans"]:
        for ss in rs["scopeSpans"]:
            for span in ss["spans"]:
                fix(span)
    return request


def test_encoding_matches_the_official_otlp_encoder():
    spans = make_spans()
    ours = ids_to_base64(json.loads(json.dumps(impact_exporter.encode_spans(spans))))
    official = MessageToDict(official_encode(spans), use_integers_for_enums=True)
    assert canon(ours) == canon(official)


def test_integers_beyond_64_bits_are_kept_as_strings():
    # The official encoder logs and drops such an attribute; we keep what the application recorded.
    from impact_instrumentation import otlp

    assert otlp.any_value(2**70) == {"stringValue": str(2**70)}
    assert otlp.any_value(2**63 - 1) == {"intValue": str(2**63 - 1)}
    assert otlp.any_value(-(2**63)) == {"intValue": str(-(2**63))}
    assert otlp.any_value(float("nan")) == {"stringValue": "nan"}
    assert otlp.any_value(True) == {"boolValue": True}
    assert otlp.any_value(b"ab") == {"bytesValue": "YWI="}
    assert otlp.any_value({"a": [1, "x"]}) == {
        "kvlistValue": {
            "values": [{"key": "a", "value": {"arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "x"}]}}}]
        }
    }


def test_ids_are_hex_strings_as_the_otlp_json_rule_requires():
    spans = make_spans()
    encoded = impact_exporter.encode_spans(spans)
    span = encoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    ctx = spans[0].get_span_context()
    assert span["traceId"] == format(ctx.trace_id, "032x")
    assert span["spanId"] == format(ctx.span_id, "016x")


def test_spans_are_grouped_by_resource_and_scope():
    encoded = impact_exporter.encode_spans(make_spans())
    assert len(encoded["resourceSpans"]) == 1
    scopes = {ss["scope"]["name"]: len(ss["spans"]) for ss in encoded["resourceSpans"][0]["scopeSpans"]}
    assert scopes == {"opentelemetry.instrumentation.test": 2, "other.scope": 2}


def test_export_posts_gzip_json_to_the_traces_path(sink):
    exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url})
    assert exporter.export(make_spans()) is SpanExportResult.SUCCESS
    (request,) = sink.requests
    assert request["path"] == "/v1/traces"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Content-Encoding"] == "gzip"
    assert request["headers"]["User-Agent"].startswith("impact-instrumentation/")
    assert {s["name"] for s in sink.spans()} == {"chat gpt-4o-mini", "child", "producer", "consumer"}


def test_content_arrives_on_the_span(sink):
    exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url})
    exporter.export(make_spans())
    chat = next(s for s in sink.spans() if s["name"] == "chat gpt-4o-mini")
    attrs = {a["key"]: a["value"] for a in chat["attributes"]}
    assert attrs["gen_ai.input.messages"] == {"stringValue": '[{"role":"user","content":"hi there"}]'}
    assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "7"}


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"IMPACT_OTLP_ENDPOINT": "http://gw:4318"}, "http://gw:4318/v1/traces"),
        ({"IMPACT_OTLP_ENDPOINT": "http://gw:4318/"}, "http://gw:4318/v1/traces"),
        # In the kit the workload's own OTEL_EXPORTER_* variables are never ours to use.
        (
            {"IMPACT_OTLP_ENDPOINT": "http://gw:4318", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://theirs:4318"},
            "http://gw:4318/v1/traces",
        ),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingest.impact.ai"}, "https://ingest.impact.ai/v1/traces"),
        (
            {
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://x/custom",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://y",
            },
            "https://x/custom",
        ),
        ({}, "http://localhost:4318/v1/traces"),
    ],
)
def test_endpoint_resolution(environ, expected):
    assert impact_exporter.ImpactSpanExporter(environ=environ).url == expected


def test_standard_headers_apply_only_outside_the_kit():
    outside = impact_exporter.ImpactSpanExporter(
        environ={
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingest.impact.ai",
            "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer%20abc,x-team=a%2Cb",
        }
    )
    assert outside.headers["authorization"] == "Bearer abc"
    assert outside.headers["x-team"] == "a,b"
    inside = impact_exporter.ImpactSpanExporter(
        environ={
            "IMPACT_OTLP_ENDPOINT": "http://gw:4318",
            "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer abc",
        }
    )
    assert "authorization" not in {k.lower() for k in inside.headers}


def test_a_transient_failure_is_retried_once_and_then_succeeds():
    sink = Sink(statuses=[503, 200])
    try:
        exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url}, sleep=lambda s: None)
        assert exporter.export(make_spans()) is SpanExportResult.SUCCESS
        assert len(sink.requests) == 2
    finally:
        sink.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413])
def test_permanent_rejections_are_not_retried(status):
    sink = Sink(statuses=[status, 200])
    try:
        exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url}, sleep=lambda s: None)
        assert exporter.export(make_spans()) is SpanExportResult.FAILURE
        assert len(sink.requests) == 1
    finally:
        sink.close()


def test_persistent_failure_returns_failure_without_raising():
    sink = Sink(statuses=[500] * 10)
    try:
        exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url}, sleep=lambda s: None)
        assert exporter.export(make_spans()) is SpanExportResult.FAILURE
        assert len(sink.requests) == 2  # the attempt, and one retry
    finally:
        sink.close()


class Down:
    """A `post` that gets no answer, as with a gateway that hangs: it records how it was asked."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, payload, **kwargs):
        self.calls.append(kwargs)
        raise TimeoutError("timed out")


def test_inside_the_kit_an_attempt_waits_two_seconds_and_is_tried_once_more():
    # A gateway that hangs costs a batch at most 2 + 0.5 + 2 seconds: it was 5 + 0.5 + 5 + 1 + 5.
    post, sleeps = Down(), []
    exporter = impact_exporter.ImpactSpanExporter(
        environ={"IMPACT_OTLP_ENDPOINT": "http://gw:4318"}, sleep=sleeps.append, post=post
    )
    assert exporter.export(make_spans()) is SpanExportResult.FAILURE
    assert [call["timeout"] for call in post.calls] == [2.0, 2.0]
    assert sleeps == [0.5]


def test_outside_the_kit_the_timeout_is_the_one_the_workload_configured():
    post = Down()
    exporter = impact_exporter.ImpactSpanExporter(
        environ={"OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingest.impact.ai", "OTEL_EXPORTER_OTLP_TIMEOUT": "3000"},
        sleep=lambda s: None,
        post=post,
    )
    exporter.export(make_spans())
    assert {call["timeout"] for call in post.calls} == {3.0}


def test_once_the_process_is_exiting_a_failed_attempt_is_not_tried_again():
    post, sleeps = Down(), []
    exporter = impact_exporter.ImpactSpanExporter(
        environ={"IMPACT_OTLP_ENDPOINT": "http://gw:4318"}, sleep=sleeps.append, post=post
    )
    exporter.begin_shutdown()
    assert exporter.export(make_spans()) is SpanExportResult.FAILURE
    assert len(post.calls) == 1
    assert sleeps == []


def test_an_attempt_that_is_already_waiting_to_try_again_stops_when_the_exit_begins():
    exporter = None

    def exit_begins_during_the_wait(seconds):
        exporter.begin_shutdown()

    post = Down()
    exporter = impact_exporter.ImpactSpanExporter(
        environ={"IMPACT_OTLP_ENDPOINT": "http://gw:4318"}, sleep=exit_begins_during_the_wait, post=post
    )
    assert exporter.export(make_spans()) is SpanExportResult.FAILURE
    assert len(post.calls) == 1


def test_the_wait_between_attempts_ends_early_when_the_exit_begins():
    # The default wait is not time.sleep(0.5): a process that is exiting does not sit it out.
    exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": "http://gw:4318"}, post=Down())
    threading.Timer(0.1, exporter.begin_shutdown).start()
    started = time.monotonic()
    assert exporter.export(make_spans()) is SpanExportResult.FAILURE
    assert time.monotonic() - started < 0.4


def test_a_healthy_gateway_is_still_given_every_batch_while_exiting(sink):
    exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url})
    exporter.begin_shutdown()
    assert exporter.export(make_spans()) is SpanExportResult.SUCCESS
    assert exporter.export(make_spans()) is SpanExportResult.SUCCESS
    assert len(sink.requests) == 2


def test_unreachable_endpoint_fails_quickly_and_quietly(caplog):
    # Fail open: ingest being down must never reach the application. Port 9 on loopback refuses connections.
    exporter = impact_exporter.ImpactSpanExporter(
        environ={"IMPACT_OTLP_ENDPOINT": "http://127.0.0.1:9", "OTEL_EXPORTER_OTLP_TIMEOUT": "1000"},
        sleep=lambda s: None,
    )
    started = time.monotonic()
    with caplog.at_level("DEBUG"):
        assert exporter.export(make_spans()) is SpanExportResult.FAILURE
        assert exporter.export(make_spans()) is SpanExportResult.FAILURE
    assert time.monotonic() - started < 5
    warnings = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    assert len(warnings) == 1  # logged once, not for every batch


def test_shutdown_stops_exporting(sink):
    exporter = impact_exporter.ImpactSpanExporter(environ={"IMPACT_OTLP_ENDPOINT": sink.url})
    exporter.shutdown()
    assert exporter.export(make_spans()) is SpanExportResult.FAILURE
    assert sink.requests == []


def test_force_flush_is_a_no_op_that_reports_success():
    assert impact_exporter.ImpactSpanExporter(environ={}).force_flush() is True


def test_exporter_class_is_a_span_exporter():
    from opentelemetry.sdk.trace.export import SpanExporter

    assert issubclass(impact_exporter.ImpactSpanExporter, SpanExporter)

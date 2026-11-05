# SPDX-License-Identifier: Apache-2.0

"""The `impact.setup` span: one report per process start, sent directly on the standard library (C8)."""

from __future__ import annotations

import gzip
import json
import threading
import time

import pytest

from helpers import Sink
from impact_instrumentation import setup_report

KIT = "IMPACT_OTLP_ENDPOINT"
PLACEMENT = {
    "OTEL_SERVICE_NAME": "support-assistant",
    "OTEL_RESOURCE_ATTRIBUTES": (
        "k8s.container.name=app,k8s.deployment.name=support-assistant,k8s.namespace.name=shop,"
        "k8s.pod.name=support-assistant-6d9c-x2,service.version=1.4%2B2"
    ),
}


def attrs(items):
    return {a["key"]: a["value"] for a in items}


def test_payload_is_one_internal_span_named_impact_setup():
    payload = setup_report.build("pre-existing", "ddtrace 2.9.1", environ=PLACEMENT, now_ns=1_700_000_000_123_456_789)
    (resource_spans,) = payload["resourceSpans"]
    (scope_spans,) = resource_spans["scopeSpans"]
    (span,) = scope_spans["spans"]
    assert span["name"] == "impact.setup"
    assert span["kind"] == 1
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    assert "parentSpanId" not in span
    assert span["startTimeUnixNano"] == span["endTimeUnixNano"] == "1700000000123456789"
    assert scope_spans["scope"]["name"] == "impact.setup"


def test_state_and_detail_are_attributes():
    payload = setup_report.build("pre-existing", "ddtrace 2.9.1", environ=PLACEMENT)
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    got = attrs(span["attributes"])
    assert got["impact.setup.state"] == {"stringValue": "pre-existing"}
    assert got["impact.setup.detail"] == {"stringValue": "ddtrace 2.9.1"}
    assert "impact.setup.package.version" in got
    assert "impact.setup.python.version" in got


def test_extra_attributes_are_added():
    payload = setup_report.build(
        "active", "", environ=PLACEMENT, extra={"impact.setup.instrumentations": "openai,anthropic,fastapi"}
    )
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert attrs(span["attributes"])["impact.setup.instrumentations"] == {"stringValue": "openai,anthropic,fastapi"}


def test_the_frameworks_and_the_untraced_ones_are_string_arrays():
    payload = setup_report.build(
        "active",
        "",
        environ=PLACEMENT,
        extra={
            "impact.setup.frameworks": ["crewai 1.15.35", "openai 3.42.1"],
            "impact.setup.untraced": ["crewai 1.15.35"],
        },
    )
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    got = attrs(span["attributes"])
    assert got["impact.setup.frameworks"] == {
        "arrayValue": {"values": [{"stringValue": "crewai 1.15.35"}, {"stringValue": "openai 3.42.1"}]}
    }
    assert got["impact.setup.untraced"] == {"arrayValue": {"values": [{"stringValue": "crewai 1.15.35"}]}}


def test_nothing_found_is_an_empty_array_not_a_missing_attribute():
    payload = setup_report.build("active", "", environ=PLACEMENT, extra={"impact.setup.untraced": []})
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert attrs(span["attributes"])["impact.setup.untraced"] == {"arrayValue": {"values": []}}


def test_resource_carries_the_placement_the_operator_set():
    payload = setup_report.build("active", "", environ=PLACEMENT)
    resource = attrs(payload["resourceSpans"][0]["resource"]["attributes"])
    assert resource["service.name"] == {"stringValue": "support-assistant"}
    assert resource["k8s.namespace.name"] == {"stringValue": "shop"}
    assert resource["k8s.deployment.name"] == {"stringValue": "support-assistant"}
    assert resource["k8s.pod.name"] == {"stringValue": "support-assistant-6d9c-x2"}
    assert resource["service.version"] == {"stringValue": "1.4+2"}  # percent-decoded, as the SDK does
    assert resource["telemetry.distro.name"] == {"stringValue": "impact-instrumentation"}


def test_detail_is_bounded():
    payload = setup_report.build("failed", "x" * 5000, environ=PLACEMENT)
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(attrs(span["attributes"])["impact.setup.detail"]["stringValue"]) <= setup_report.MAX_DETAIL


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError, match="state"):
        setup_report.build("sleeping", "", environ=PLACEMENT)


def test_report_is_sent_to_the_gateway_as_gzip_json():
    sink = Sink()
    try:
        env = {KIT: sink.url, **PLACEMENT}
        thread = setup_report.send_in_background(
            setup_report.build("pre-existing", "ddtrace", environ=env), environ=env
        )
        assert thread is not None
        thread.join(10)
        (request,) = sink.requests
        assert request["path"] == "/v1/traces"
        assert request["headers"]["Content-Encoding"] == "gzip"
        body = json.loads(gzip.decompress(request["body"]))
        assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "impact.setup"
    finally:
        sink.close()


def test_a_stood_down_workload_reports_only_to_the_gateway():
    # OTEL_EXPORTER_OTLP_ENDPOINT now belongs to the other tracer; the report must never go there.
    sink = Sink()
    try:
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": sink.url, **PLACEMENT}
        payload = setup_report.build("pre-existing", "ddtrace", environ=env)
        assert setup_report.send_in_background(payload, environ=env) is None
        assert sink.requests == []
    finally:
        sink.close()


def test_outside_the_kit_an_active_report_follows_the_standard_endpoint():
    sink = Sink()
    try:
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": sink.url, **PLACEMENT}
        thread = setup_report.send_in_background(setup_report.build("active", "", environ=env), environ=env)
        assert thread is not None
        thread.join(10)
        assert len(sink.requests) == 1
    finally:
        sink.close()


def test_an_unreachable_gateway_is_silent():
    env = {KIT: "http://127.0.0.1:9", **PLACEMENT}
    thread = setup_report.send_in_background(setup_report.build("active", "", environ=env), environ=env)
    assert thread is not None
    thread.join(10)
    assert not thread.is_alive()


def test_at_exit_the_process_waits_half_a_second_at_most_for_a_report_that_hangs():
    # A gateway that does not answer holds the sender thread; `python -c pass` waited 3 s for it.
    release = threading.Event()
    thread = threading.Thread(target=release.wait, args=(30,), daemon=True)
    thread.start()
    started = time.monotonic()
    setup_report._join(thread)
    took = time.monotonic() - started
    release.set()
    assert 0.3 <= took < 1.0


def test_the_report_is_sent_again_after_a_fifth_of_a_second(monkeypatch):
    from impact_instrumentation import otlp

    attempts, sleeps = [], []

    def refused(url, payload, **kwargs):
        attempts.append(kwargs["timeout"])
        raise ConnectionRefusedError

    monkeypatch.setattr(otlp, "post", refused)
    monkeypatch.setattr(setup_report.time, "sleep", sleeps.append)
    setup_report._run("http://gw:4318/v1/traces", {}, {}, otlp.timeout_seconds({KIT: "http://gw:4318"}))
    assert attempts == [2.0, 2.0]
    assert sleeps == [0.2]


def test_the_sender_thread_is_a_daemon_so_it_never_holds_the_process_open():
    env = {KIT: "http://127.0.0.1:9", **PLACEMENT}
    thread = setup_report.send_in_background(setup_report.build("active", "", environ=env), environ=env)
    assert thread is not None
    assert thread.daemon
    thread.join(10)


def test_a_report_is_claimed_once_per_container(tmp_path):
    env = {"IMPACT_STATE_DIR": str(tmp_path)}
    assert setup_report.claim("active", "fastapi", environ=env, root="/r") is True
    assert setup_report.claim("active", "fastapi", environ=env, root="/r") is False
    assert setup_report.claim("active", "fastapi,openai", environ=env, root="/r") is True  # a different report
    assert setup_report.claim("failed", "boom", environ=env, root="/r") is True
    assert setup_report.claim("failed", "boom", environ=env, root="/other") is True  # another payload


def test_a_report_with_other_frameworks_is_a_different_report(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(setup_report, "send_in_background", lambda payload, *, environ: sent.append(payload))
    env = {"IMPACT_STATE_DIR": str(tmp_path)}
    one = {"impact.setup.frameworks": ["openai 3.42.1"], "impact.setup.untraced": []}
    two = {"impact.setup.frameworks": ["openai 3.42.1", "mcp 2.2.0"], "impact.setup.untraced": ["mcp 2.2.0"]}
    setup_report.report("active", "", environ=env, root="/r", extra=one)
    setup_report.report("active", "", environ=env, root="/r", extra=one)
    setup_report.report("active", "", environ=env, root="/r", extra=two)
    assert len(sent) == 2


def test_when_nothing_is_writable_the_report_is_sent_anyway(tmp_path):
    env = {"IMPACT_STATE_DIR": str(tmp_path / "missing" / "dir")}
    assert setup_report.claim("active", "x", environ=env, root=str(tmp_path / "also-missing")) is True
    assert setup_report.claim("active", "x", environ=env, root=str(tmp_path / "also-missing")) is True


def test_the_marker_falls_back_to_the_payload_directory(tmp_path):
    root = tmp_path / "payload"
    root.mkdir()
    env = {"IMPACT_STATE_DIR": str(tmp_path / "missing")}
    assert setup_report.claim("active", "x", environ=env, root=str(root)) is True
    assert setup_report.claim("active", "x", environ=env, root=str(root)) is False


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ("k8s.deployment.name=support-assistant,k8s.pod.name=support-assistant-6d9c-x2", "support-assistant"),
        ("k8s.statefulset.name=db,k8s.pod.name=db-0", "db"),
        ("k8s.daemonset.name=agent,k8s.pod.name=agent-x", "agent"),
        ("k8s.cronjob.name=nightly,k8s.job.name=nightly-2911,k8s.pod.name=nightly-2911-ab", "nightly"),
        ("k8s.job.name=migrate,k8s.pod.name=migrate-ab", "migrate"),
        ("k8s.pod.name=standalone", "standalone"),
    ],
)
def test_the_service_name_defaults_to_the_workload_when_the_operator_did_not_set_one(attributes, expected):
    from impact_instrumentation import otlp

    # The kit's Instrumentation leaves OTEL_SERVICE_NAME out (a stood-down tracer would obey it).
    env = {"OTEL_RESOURCE_ATTRIBUTES": attributes}
    assert otlp.resource_attributes(env)["service.name"] == expected
    assert otlp.default_service_name(env) == expected


def test_an_explicit_service_name_is_never_replaced():
    from impact_instrumentation import otlp

    named = {"OTEL_SERVICE_NAME": "mine", "OTEL_RESOURCE_ATTRIBUTES": "k8s.deployment.name=support-assistant"}
    assert otlp.resource_attributes(named)["service.name"] == "mine"
    assert otlp.default_service_name(named) is None
    in_attributes = {"OTEL_RESOURCE_ATTRIBUTES": "service.name=theirs,k8s.deployment.name=support-assistant"}
    assert otlp.resource_attributes(in_attributes)["service.name"] == "theirs"
    assert otlp.default_service_name(in_attributes) is None

# SPDX-License-Identifier: Apache-2.0

"""An injected workload on every Java the image is built for: its request roots and HTTP client calls reach the
gateway with the workload's name, health probes make no spans, the agent sends no metrics and no application log line
leaves the pod, and the setup report says the agent is active."""

from __future__ import annotations

import time

import pytest
from java_support import (
    JAVAS,
    agent_spans,
    http,
    only,
    paths,
    setup_reports,
    wait_ready,
    wait_until,
)

pytestmark = pytest.mark.java

SERVER, CLIENT = 2, 3  # OTLP span kinds


@pytest.mark.parametrize("java", JAVAS)
def test_request_roots_and_client_calls_arrive_and_probes_do_not(start_app, mock, java):
    app = start_app(java)
    wait_ready(app)
    for _ in range(3):
        assert http("GET", f"{app.url}/healthz")[0] == 200
    assert http("POST", f"{app.url}/log", {})[0] == 200
    status, answer = http("POST", f"{app.url}/chat", {})
    assert status == 200 and "Hello from the stub." in str(answer)

    def chat_spans():
        spans = agent_spans(mock)
        server = [s for s in spans if s["kind"] == SERVER and s["attrs"].get("url.path") == "/chat"]
        client = [s for s in spans if s["kind"] == CLIENT and "/v1/chat/completions" in str(s["attrs"].get("url.full"))]
        return (server, client) if server and client else None

    server, client = wait_until(chat_spans, timeout=60, message=lambda: f"the /chat spans\n{app.logs()}")
    time.sleep(1.5)  # anything else the agent would send arrives in the next batch
    spans = agent_spans(mock)
    resource = server[0]["resource"]
    assert resource["service.name"] == "support-assistant", resource  # the workload's name, from the Operator
    assert resource["telemetry.distro.name"] == "impact-instrumentation"
    assert resource["k8s.namespace.name"] == "shop"
    assert server[0]["scope"].startswith("io.opentelemetry.java-http-server")
    assert client[0]["scope"].startswith("io.opentelemetry.java-http-client")

    # health probes make no spans; the agent sends no metrics; the application's log line stays in the pod
    assert not [s for s in spans if "healthz" in str(s["attrs"].get("url.path", ""))]
    assert [p for p in paths(mock) if "metrics" in p] == []
    assert [p for p in paths(mock) if p.endswith("/v1/logs")] == []

    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "active"
    assert report["attrs"]["impact.setup.java.version"].split(".")[0] == java
    assert "java-http-server" in report["attrs"]["impact.setup.instrumentations"].split(",")
    assert report["attrs"]["impact.setup.frameworks"] == []
    assert report["resource"]["service.name"] == "support-assistant"

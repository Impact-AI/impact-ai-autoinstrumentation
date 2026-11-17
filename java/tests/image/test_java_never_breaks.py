# SPDX-License-Identifier: Apache-2.0

"""The application never depends on Impact AI (fail open): with the gateway unreachable, on a Java outside the range,
with nowhere to unpack the agent, or with OpenTelemetry switched off, the workload starts and serves as it would without
the kit, and the setup report says why when there is something to say."""

from __future__ import annotations

import time

import pytest
from java_support import (
    OUTSIDE,
    agent_spans,
    http,
    only,
    payload_files,
    setup_reports,
    wait_ready,
    wait_until,
)

pytestmark = pytest.mark.java


def serves(app) -> None:
    wait_ready(app)
    status, answer = http("POST", f"{app.url}/chat", {})
    assert status == 200 and "Hello from the stub." in str(answer), app.logs()


@pytest.mark.parametrize(
    "gateway",
    ["http://127.0.0.1:9", "http://192.0.2.1:4318"],  # nothing listens; nothing routes (TEST-NET-1)
    ids=["refused", "blackhole"],
)
def test_an_unreachable_gateway_does_not_stop_the_workload(start_app, gateway):
    # The export fails in the background and the report gives up after two tries.
    app = start_app("21", gateway=gateway)
    started = time.monotonic()
    serves(app)
    for _ in range(5):
        assert http("POST", f"{app.url}/chat", {}, timeout=5)[0] == 200
    assert time.monotonic() - started < 120
    assert app.is_running()


def test_a_java_outside_the_range_starts_as_without_the_kit_and_says_why(start_app, mock, payload):
    app = start_app(OUTSIDE)
    serves(app)
    wait_until(lambda: setup_reports(mock), timeout=60, message=lambda: f"the setup report\n{app.logs()}")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "failed"
    assert report["attrs"]["impact.setup.detail"].startswith(
        f"Impact AI traces Java 17, 21 and 25; this workload runs Java {OUTSIDE}."
    ), report["attrs"]["impact.setup.detail"]
    assert report["attrs"]["impact.setup.java.version"].startswith(f"{OUTSIDE}.")
    time.sleep(1.5)
    assert agent_spans(mock) == []
    assert payload_files(payload) == ["javaagent.jar"]
    assert "opentelemetry-javaagent - version" not in app.logs()


def test_nowhere_to_unpack_the_agent_starts_as_without_the_kit_and_says_why(start_app, mock):
    # The Operator's volume read-only, and no writable temporary directory: the root file system is read-only too.
    app = start_app("21", payload_mode=":ro", extra_args=["--read-only"])
    serves(app)
    wait_until(lambda: setup_reports(mock), timeout=60, message=lambda: f"the setup report\n{app.logs()}")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "failed"
    assert "no writable place for the agent" in report["attrs"]["impact.setup.detail"]
    assert agent_spans(mock) == []


@pytest.mark.parametrize(
    "env",
    [{"OTEL_SDK_DISABLED": "true"}, {"OTEL_JAVAAGENT_ENABLED": "false"}],
    ids=["OTEL_SDK_DISABLED=true", "OTEL_JAVAAGENT_ENABLED=false"],
)
def test_opentelemetry_switched_off_by_the_workload_is_respected(start_app, mock, payload, env):
    app = start_app("21", env=env)
    serves(app)
    time.sleep(2)
    assert mock.requests("/v1/traces") == []
    assert payload_files(payload) == ["javaagent.jar"]

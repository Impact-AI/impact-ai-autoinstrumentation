# SPDX-License-Identifier: Apache-2.0

"""Stand down beside another agent: a workload that already runs a Java agent of its own, an APM vendor's or an
OpenTelemetry agent, is annotated anyway. The kit's agent does not start, the workload's own agent works as before, and
the setup report names what was found. So does a workload that sets up an OpenTelemetry exporter of its own (the SDK in
its code), as the Python and Node.js images do."""

from __future__ import annotations

import time

import pytest
from java_support import (
    agent_spans,
    http,
    only,
    payload_files,
    setup_reports,
    wait_ready,
    wait_until,
)

pytestmark = pytest.mark.java

APP = ["-jar", "/app/app.jar"]
FAKE_APM = "/agents/dd-java-agent.jar"
OWN_OTEL = "/agents/opentelemetry-javaagent.jar"
OWN = "OTEL_EXPORTER_OTLP_ENDPOINT (set by the workload)"


def assert_stood_down(app, mock, payload, detail: str) -> None:
    wait_until(lambda: setup_reports(mock), timeout=60, message=lambda: f"the setup report\n{app.logs()}")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "pre-existing"
    assert report["attrs"]["impact.setup.detail"] == detail
    assert report["attrs"]["impact.setup.frameworks"] == []
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    time.sleep(1.5)
    # nothing but the report reaches impact.ai, and the kit's agent never unpacked itself
    assert agent_spans(mock) == []
    assert payload_files(payload) == ["javaagent.jar"]


@pytest.mark.parametrize(
    ("tool_options", "command", "detail"),
    [
        # what an APM vendor's admission controller does: its agent in JAVA_TOOL_OPTIONS, before the kit's
        (f"-javaagent:{FAKE_APM}", None, "dd-java-agent.jar (-javaagent in JAVA_TOOL_OPTIONS)"),
        # the workload's own start command, after the kit's
        ("", ["java", f"-javaagent:{FAKE_APM}", *APP], "dd-java-agent.jar (-javaagent on the command line)"),
    ],
    ids=["in JAVA_TOOL_OPTIONS", "on the command line"],
)
def test_beside_an_apm_vendors_agent(start_app, mock, payload, tool_options, command, detail):
    app = start_app("21", tool_options=tool_options, command=command)
    wait_ready(app)
    assert http("GET", f"{app.url}/agent")[1] == "yes", "the workload's own agent runs"
    assert_stood_down(app, mock, payload, detail)


@pytest.mark.parametrize(
    ("tool_options", "command", "detail"),
    [
        (
            "",
            ["java", f"-javaagent:{OWN_OTEL}", *APP],
            f"opentelemetry-javaagent.jar (-javaagent on the command line), {OWN}",
        ),
        (f"-javaagent:{OWN_OTEL}", None, f"opentelemetry-javaagent.jar (-javaagent in JAVA_TOOL_OPTIONS), {OWN}"),
    ],
    ids=["on the command line", "in JAVA_TOOL_OPTIONS"],
)
def test_beside_an_opentelemetry_agent_of_its_own(start_app, mock, theirs, payload, tool_options, command, detail):
    """The upstream agent has the same Premain-Class as any OpenTelemetry agent: the kit carries it nested, so the
    workload's own copy is the one that starts, with the workload's own settings, wherever it is on the line."""
    app = start_app(
        "21",
        tool_options=tool_options,
        command=command,
        env={
            "OTEL_SERVICE_NAME": "their-own",
            "OTEL_EXPORTER_OTLP_ENDPOINT": theirs.internal_url,
            "OTEL_METRICS_EXPORTER": "none",
            "OTEL_LOGS_EXPORTER": "none",
        },
    )
    wait_ready(app)
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    spans = wait_until(
        lambda: [s for s in agent_spans(theirs) if s["attrs"].get("url.path") == "/chat"],
        timeout=60,
        message=lambda: f"the workload's own spans\n{app.logs()}",
    )
    resource = spans[0]["resource"]
    assert resource["service.name"] == "their-own"
    assert resource["telemetry.distro.name"] == "opentelemetry-java-instrumentation", "the kit's extension stayed out"
    assert_stood_down(app, mock, payload, detail)


PROPERTY = "otel.exporter.otlp.traces.endpoint"


@pytest.mark.parametrize(
    ("env", "tool_options", "detail"),
    [
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "THEIRS"}, "", OWN),
        ({}, f"-D{PROPERTY}=THEIRS/v1/traces", f"{PROPERTY} (set by the workload)"),
        ({"OTEL_TRACES_EXPORTER": "zipkin"}, "", "OTEL_TRACES_EXPORTER (set by the workload)"),
    ],
    ids=["an endpoint variable", "an endpoint property", "another exporter"],
)
def test_beside_an_exporter_of_its_own(start_app, mock, theirs, payload, env, tool_options, detail):
    """No other agent, but the workload's own exporter settings: an OpenTelemetry SDK it sets up in its code. The kit's
    agent would point that pipeline at the gateway; it stands down, names the setting and never its value."""
    env = {key: value.replace("THEIRS", theirs.internal_url) for key, value in env.items()}
    app = start_app("21", env=env, tool_options=tool_options.replace("THEIRS", theirs.internal_url))
    wait_ready(app)
    assert_stood_down(app, mock, payload, detail)
    assert theirs.internal_url not in only(setup_reports(mock), "setup report")["attrs"]["impact.setup.detail"]

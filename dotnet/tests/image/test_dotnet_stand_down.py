# SPDX-License-Identifier: Apache-2.0

"""Proof first: stand down beside another profiler, under the Operator's environment rules.

The Operator keeps a workload's own CORECLR_PROFILER, CORECLR_PROFILER_PATH and CORECLR_ENABLE_PROFILING, so beside an
APM vendor's profiler the upstream profiler never loads. It still appends the upstream startup hook to
DOTNET_STARTUP_HOOKS, and that hook starts the upstream agent anyway: it only warns that "a different profiler ID was
provided". Both tracers then trace the same requests. The kit's hook, which runs first, stands the upstream agent down
through the agent's own exclusion switch, and the vendor's tracer works as before. It does the same beside OpenTelemetry
exporter settings of the workload's own, the ones the kit's Java agent counts.

What it does not reach yet: a workload that sets DOTNET_STARTUP_HOOKS itself. The Operator adds the Instrumentation's
env only where the workload has none, so the kit's hook is not named there, while the upstream hook is still appended.
test_a_workload_with_startup_hooks_of_its_own_gets_no_stand_down records that; where the kit's image puts its hook
decides it.
"""

from __future__ import annotations

import time

import pytest
from dotnet_support import OWN_HOOK, agent_spans, chat_spans, datadog_env, http, paths, wait_ready, wait_until

pytestmark = pytest.mark.dotnet

DATADOG = "Datadog.Trace.ClrProfiler.Native.so (CORECLR_PROFILER)"


def datadog_traces(theirs) -> list[str]:
    return [p for p in paths(theirs) if p.startswith("/v0.4/traces") or p.startswith("/v0.5/traces")]


def assert_stood_down(app, mock, detail: str) -> None:
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    time.sleep(2)
    logs = app.logs()
    assert f"impact.ai: stood down: {detail}" in logs, logs[-5000:]
    assert "impact.ai: chat clients wrapped" not in logs
    # the upstream agent did not start: nothing reaches impact.ai
    assert agent_spans(mock) == []
    assert "Automatic Instrumentation won't be loaded" in logs


def test_beside_an_apm_vendors_profiler(start_app, mock, theirs):
    app = start_app(env=datadog_env("glibc", theirs.internal_url))
    wait_ready(app)
    assert_stood_down(app, mock, DATADOG)
    # Datadog's tracer traces the request, as it does without the kit
    wait_until(lambda: datadog_traces(theirs), timeout=30, message=lambda: f"Datadog's traces\n{app.logs()[-5000:]}")


def test_without_the_hook_both_tracers_run(start_app, mock, theirs):
    """The baseline: the Operator's injection with the upstream agent alone. Beside Datadog's profiler the upstream
    agent starts anyway, and the same request is traced twice, by each tracer to its own backend."""
    app = start_app(
        env=datadog_env("glibc", theirs.internal_url),
        hook=False,
        spec={"OTEL_EXPORTER_OTLP_ENDPOINT": mock.internal_url},
    )
    wait_ready(app)
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    wait_until(lambda: datadog_traces(theirs), timeout=30, message=lambda: f"Datadog's traces\n{app.logs()[-5000:]}")
    wait_until(
        lambda: [s for s in agent_spans(mock) if s["attrs"].get("url.path") == "/chat"],
        timeout=30,
        message=lambda: f"the upstream agent's spans\n{app.logs()[-5000:]}",
    )
    assert "a different profiler ID was provided" in app.logs()


@pytest.mark.parametrize(
    "variable",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_TRACES_EXPORTER",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_ZIPKIN_ENDPOINT",
    ],
)
def test_beside_exporter_settings_of_its_own(start_app, mock, theirs, variable):
    """No other profiler, so the upstream profiler loads; but the workload has an OpenTelemetry pipeline of its own. The
    kit's hook stands the upstream agent down, names the setting and never its value, and the workload keeps working
    with the upstream profiler attached and idle."""
    value = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": theirs.internal_url,
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{theirs.internal_url}/v1/traces",
        "OTEL_TRACES_EXPORTER": "zipkin",
        # with the endpoint in the application's own configuration: the kit's endpoint would get the workload's key
        "OTEL_EXPORTER_OTLP_HEADERS": "x-api-key=secret-of-the-workload",
        "OTEL_EXPORTER_ZIPKIN_ENDPOINT": f"{theirs.internal_url}/api/v2/spans",
    }[variable]
    app = start_app(env={variable: value})
    wait_ready(app)
    assert_stood_down(app, mock, f"{variable} (set by the workload)")
    # nor did it start towards the workload's own backend (the fixture has no OpenTelemetry SDK of its own)
    assert agent_spans(theirs) == []
    assert value not in "\n".join(line for line in app.logs().splitlines() if line.startswith("impact.ai:"))


def test_the_kits_endpoint_inherited_from_a_parent_is_not_the_workloads(start_app, mock):
    """A process an injected workload starts inherits the OTEL_EXPORTER_OTLP_ENDPOINT the kit's hook set in its parent,
    and DOTNET_STARTUP_HOOKS with it. That value is the kit's (IMPACT_OTLP_ENDPOINT), not the workload's: the hook
    starts and the model call is traced."""
    app = start_app(env={"OTEL_EXPORTER_OTLP_ENDPOINT": mock.internal_url})
    wait_ready(app)
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    wait_until(lambda: chat_spans(mock), timeout=60, message=lambda: f"the chat span\n{app.logs()[-5000:]}")
    logs = app.logs()
    assert "impact.ai: started" in logs
    assert "impact.ai: stood down" not in logs


def test_a_workload_with_startup_hooks_of_its_own_gets_no_stand_down(start_app, mock, theirs):
    """The gap the kit's image must close, as it is today: the workload names a startup hook of its own (as an APM agent
    that attaches by one does) and runs Datadog's profiler. The Operator keeps the workload's DOTNET_STARTUP_HOOKS, so
    the kit's hook is not named, and appends the upstream hook: the upstream agent starts beside Datadog's tracer, and
    nothing says so. When the image puts the kit's hook where the Operator always names it, this test fails and turns
    into a stand-down test."""
    env = {**datadog_env("glibc", theirs.internal_url), "DOTNET_STARTUP_HOOKS": OWN_HOOK}
    app = start_app(env=env)
    wait_ready(app)
    assert http("POST", f"{app.url}/chat", {})[0] == 200
    wait_until(lambda: datadog_traces(theirs), timeout=30, message=lambda: f"Datadog's traces\n{app.logs()[-5000:]}")
    logs = app.logs()
    assert "own startup hook: ran" in logs, logs[-5000:]
    assert "impact.ai:" not in logs
    # the upstream agent started beside Datadog's profiler, as in test_without_the_hook_both_tracers_run
    assert "a different profiler ID was provided" in logs
    assert "Automatic Instrumentation won't be loaded" not in logs

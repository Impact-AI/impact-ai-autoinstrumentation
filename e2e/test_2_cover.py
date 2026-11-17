# SPDX-License-Identifier: Apache-2.0

"""Cover a Python workload without code, and stand down beside another tracer."""

from __future__ import annotations

import json

from kube import (
    CLOUD_ON_SPANS,
    CLUSTER_NAME,
    IMAGE_PREFIX,
    PLANTED,
    TAG,
    call_app,
    cover,
    fixture_frameworks,
    get,
    kubectl,
    running_pod,
    wait_until,
)

INJECT = "instrumentation.opentelemetry.io/inject-python"
VALUE = "impact-ai/impact"  # what the per-row command sets
INIT = "opentelemetry-auto-instrumentation-python"
PROMPT, ANSWER = "hi there", "Hello from the stub."
OPERATOR_DEFAULTS = {
    "OTEL_TRACES_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_LOGS_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SERVICE_NAME",
}


def annotate(name: str, namespace: str = "shop") -> None:
    """The per-row command: annotate the pod template, which restarts the workload (see kube.cover)."""
    cover(name, namespace, annotate=True)


def container_env(pod: dict) -> dict:
    return {e["name"]: e for e in pod["spec"]["containers"][0].get("env", [])}


def runtime_env(deployment: str, namespace: str = "shop") -> dict:
    """The environment the application process really sees."""
    code = (
        "import json, os; "
        "print(json.dumps({k: v for k, v in os.environ.items() if k.startswith(('OTEL_', 'DD_', 'IMPACT_'))}))"
    )
    out = kubectl("-n", namespace, "exec", f"deploy/{deployment}", "-c", "app", "--", "python", "-c", code)
    return json.loads(out.stdout.strip().splitlines()[-1])


def spans_of(world, deployment: str) -> list[dict]:
    return [s for s in world.mock.spans() if s["resource"].get("k8s.deployment.name") == deployment]


def setup_reports(world, deployment: str) -> list[dict]:
    return [s for s in spans_of(world, deployment) if s["name"] == "impact.setup"]


def test_the_row_command_covers_the_workload_with_no_change_to_its_image_or_code(world):
    before = get("-n", "shop", "deployment", "support-assistant")["spec"]["template"]["spec"]["containers"][0]
    annotate("support-assistant")
    after = get("-n", "shop", "deployment", "support-assistant")["spec"]["template"]["spec"]["containers"][0]
    assert after["image"] == before["image"]
    assert after["command"] == before["command"] and after.get("args") == before.get("args")
    pod = running_pod("shop", "app=support-assistant")
    (init,) = pod["spec"]["initContainers"]
    assert init["name"] == INIT
    assert init["image"] == f"{IMAGE_PREFIX}/python:{TAG}"
    assert init["command"][:2] == ["cp", "-r"]
    env = container_env(pod)
    assert env["PYTHONPATH"]["value"].startswith("/otel-auto-instrumentation-python/")
    assert env["IMPACT_OTLP_ENDPOINT"]["value"] == "http://impact-ai-gateway.impact-ai.svc:4318"
    # The Operator's own defaults for the exporters and the service name never reach the workload.
    assert OPERATOR_DEFAULTS.isdisjoint(env) or all("valueFrom" in env[n] for n in OPERATOR_DEFAULTS & set(env))


def test_the_workloads_llm_spans_arrive_with_provider_model_tokens_placement_and_content(world):
    for provider in ("openai", "anthropic", "bedrock"):
        assert call_app("shop", "support-assistant", provider) == {"answer": ANSWER}

    def llm_spans():
        found = [s for s in spans_of(world, "support-assistant") if "gen_ai.request.model" in s["attrs"]]
        return (
            found
            if {s["attrs"]["gen_ai.request.model"] for s in found}
            >= {"gpt-4o-mini", "claude-stub", "claude-3-haiku-20240307-v1:0"}
            else None
        )

    spans = wait_until(llm_spans, timeout=120, message="the three LLM spans at impact.ai")
    for span in spans:
        assert span["attrs"]["gen_ai.usage.input_tokens"] == 7
        assert span["attrs"]["gen_ai.usage.output_tokens"] == 5
        assert PROMPT in span["attrs"]["gen_ai.input.messages"] and ANSWER in span["attrs"]["gen_ai.output.messages"]
        resource = span["resource"]
        assert resource["k8s.cluster.uid"] == world.cluster_uid  # added by the gateway
        assert resource["k8s.cluster.name"] == CLUSTER_NAME  # the reference in it was not read as one
        assert {k: resource.get(k) for k in CLOUD_ON_SPANS} == CLOUD_ON_SPANS  # the install's cloud identity
        assert resource["k8s.namespace.name"] == "shop"
        assert resource["k8s.deployment.name"] == "support-assistant"
        assert resource["service.name"] == "support-assistant"
    report = setup_reports(world, "support-assistant")[-1]
    assert report["attrs"]["impact.setup.state"] == "active"
    assert {"openai", "anthropic", "boto3", "fastapi"} <= set(
        report["attrs"]["impact.setup.instrumentations"].split(",")
    )
    # The AI libraries found in the workload's environment, with versions, and none the image cannot trace.
    assert report["attrs"]["impact.setup.frameworks"] == fixture_frameworks()
    assert report["attrs"]["impact.setup.untraced"] == []


def test_health_probe_requests_are_not_traced(world):
    # the readiness probe calls /healthz every 3 seconds
    names = [s["name"] for s in spans_of(world, "support-assistant")]
    assert not [n for n in names if "healthz" in n]


def test_the_reporter_sees_the_workload_injected_and_still_no_value(world):
    def injected():
        for snapshot in reversed(world.mock.inventories()):
            for w in snapshot["workloads"]:
                if w["name"] == "support-assistant" and w["kind"] == "Deployment":
                    return w if w["containers"][0]["injected"]["python"] else None
        return None

    covered = wait_until(injected, timeout=90, message="a snapshot showing support-assistant injected")
    assert covered["annotations"] == {INJECT: VALUE}
    assert all(p.encode() not in body for body in world.mock.bodies() for p in PLANTED)


def assert_stood_down(world, deployment: str, detail_starts: str) -> None:
    reports = wait_until(
        lambda: setup_reports(world, deployment), timeout=120, message=f"the setup report of {deployment}"
    )
    assert {r["attrs"]["impact.setup.state"] for r in reports} == {"pre-existing"}
    assert reports[0]["attrs"]["impact.setup.detail"].startswith(detail_starts), reports[0]["attrs"]
    # A stood-down workload still says which AI libraries it has.
    assert reports[0]["attrs"]["impact.setup.frameworks"] == fixture_frameworks()
    # nothing but the report reaches impact.ai from this workload
    assert {s["name"] for s in spans_of(world, deployment)} == {"impact.setup"}
    pod = running_pod("shop", f"app={deployment}")
    assert [c["name"] for c in pod["spec"].get("initContainers", [])] == [INIT]  # injected, and the package stood down


def test_datadog_started_in_code_keeps_its_own_traces(world):
    annotate("legacy-tracer")
    call_app("shop", "legacy-tracer", "openai")
    assert_stood_down(world, "legacy-tracer", "ddtrace ")
    wait_until(
        lambda: [r for r in world.theirs.requests() if r["path"].startswith("/v0.")],
        timeout=120,
        message="the workload's traces at its own Datadog agent",
    )
    # impact.ai got no LLM span of this workload
    assert not [s for s in spans_of(world, "legacy-tracer") if "gen_ai.request.model" in s["attrs"]]
    # and the Operator's OTEL_* defaults, which make ddtrace 4 export OTLP to localhost, never reached the process
    assert OPERATOR_DEFAULTS.isdisjoint(runtime_env("legacy-tracer"))


def test_an_entry_point_that_runs_opentelemetry_instrument_keeps_its_own_traces(world):
    annotate("own-otel")
    call_app("shop", "own-otel", "openai")
    assert_stood_down(world, "own-otel", "opentelemetry-instrument (launcher)")
    spans = wait_until(
        lambda: [s for s in world.theirs.spans() if s["name"].startswith("POST /chat/openai")],
        timeout=120,
        message="the workload's server span at its own collector",
    )
    assert spans[0]["resource"]["service.name"] == "own-otel"  # its own name, not one derived by the kit
    env = runtime_env("own-otel")
    assert "OTEL_TRACES_EXPORTER" not in env  # the one default it did not set itself was not added either
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://theirs.impact-mock.svc:8080"

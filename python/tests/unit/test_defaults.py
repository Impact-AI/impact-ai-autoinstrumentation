# SPDX-License-Identifier: Apache-2.0

"""C7: environment defaults, and health-probe requests that are never traced."""

from __future__ import annotations

import pytest
from opentelemetry.util.http import get_excluded_urls

from impact_instrumentation import defaults

PROBES = [
    "http://app:8000/healthz",
    "http://app:8000/health",
    "http://app:8000/health/",
    "http://app:8000/health?verbose=1",
    "http://app:8000/healthcheck",
    "http://app:8000/livez",
    "http://app:8000/live",
    "http://app:8000/liveness",
    "http://app:8000/readyz",
    "http://app:8000/ready",
    "http://app:8000/readiness",
    "http://app:8000/ping",
    "http://app:8000/_health",
    "http://app:8000/-/healthy",
    "http://app:8000/-/ready",
    "http://app:8000/api/v1/health",
    "http://app:8000/healthz/ready",
    "http://app:8000/metrics",
]
NOT_PROBES = [
    "http://app:8000/",
    "http://app:8000/chat",
    "http://app:8000/v1/chat/completions",
    "http://app:8000/api/healthcare",
    "http://app:8000/healthy-food",
    "http://app:8000/pingpong",
    "http://app:8000/ready-made",
    "http://app:8000/metrics-dashboard",
    "http://healthcheck.example.com/chat",
]


def excluded(environ: dict[str, str], instrumentation: str = "FASTAPI"):
    import os

    old = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environ)
        return get_excluded_urls(instrumentation)
    finally:
        os.environ.clear()
        os.environ.update(old)


@pytest.mark.parametrize("instrumentation", ["FASTAPI", "FLASK", "STARLETTE", "DJANGO", "FALCON", "PYRAMID"])
def test_health_probes_are_excluded_for_every_server_instrumentation(instrumentation):
    env = {}
    defaults.apply(env)
    exclude = excluded(env, instrumentation)
    assert [u for u in PROBES if not exclude.url_disabled(u)] == []


def test_ordinary_requests_are_traced():
    env = {}
    defaults.apply(env)
    exclude = excluded(env)
    assert [u for u in NOT_PROBES if exclude.url_disabled(u)] == []


def test_defaults_match_c7():
    assert defaults.DEFAULTS["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    assert defaults.DEFAULTS["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_ONLY"
    assert set(defaults.DEFAULTS) == {
        "OTEL_SEMCONV_STABILITY_OPT_IN",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        "OTEL_PYTHON_EXCLUDED_URLS",
    }


def test_apply_never_overrides_a_value_the_workload_set():
    env = {
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
        "OTEL_PYTHON_EXCLUDED_URLS": "internal-only",
    }
    changed = defaults.apply(env)
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "NO_CONTENT"
    assert env["OTEL_PYTHON_EXCLUDED_URLS"] == "internal-only"
    assert env["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    assert "OTEL_SEMCONV_STABILITY_OPT_IN" in changed
    assert "OTEL_PYTHON_EXCLUDED_URLS" not in changed


@pytest.mark.parametrize("mode", ["NO_CONTENT", "no_content", "false", "EVENT_ONLY"])
def test_a_content_free_mode_also_silences_openllmetry(mode):
    # OpenLLMetry's Bedrock package has its own switch; one setting must govern all three providers.
    env = {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": mode}
    defaults.apply(env)
    assert env["TRACELOOP_TRACE_CONTENT"] == "false"


@pytest.mark.parametrize("mode", [None, "SPAN_ONLY", "SPAN_AND_EVENT", "true"])
def test_content_modes_that_put_content_on_the_span_leave_openllmetry_on(mode):
    env = {} if mode is None else {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": mode}
    defaults.apply(env)
    assert "TRACELOOP_TRACE_CONTENT" not in env


def test_an_explicit_traceloop_setting_wins():
    env = {
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
        "TRACELOOP_TRACE_CONTENT": "true",
    }
    defaults.apply(env)
    assert env["TRACELOOP_TRACE_CONTENT"] == "true"


def test_patterns_contain_no_comma():
    # The variable is a comma-separated list: a comma inside a pattern would split it.
    assert all("," not in p for p in defaults.PROBE_PATTERNS)

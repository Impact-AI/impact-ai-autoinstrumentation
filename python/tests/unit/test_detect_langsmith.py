# SPDX-License-Identifier: Apache-2.0

"""LangSmith: the package starts beside its default tracing and stands down in its OpenTelemetry mode.

LangSmith's default tracing uses callbacks and wrappers and never touches the OpenTelemetry provider, so it is no
finding (python/tests/image/test_langsmith.py proves that LangSmith receives the same runs beside the package). Its
OpenTelemetry mode makes the SDK own the process's provider; then the workload owns its tracing. These cases follow how
the langsmith SDK 0.14.17 resolves its mode (`_resolve_tracing_mode` in langsmith/client.py).
"""

from __future__ import annotations

import pytest

from impact_instrumentation import detect

TRACING = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "lsv2_pt_planted", "LANGSMITH_PROJECT": "support"}


def find(tmp_path, environ):
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    for dist in ("langsmith-0.14.17", "langchain_core-1.6.13", "opentelemetry_sdk-1.45.2"):
        (site / f"{dist}.dist-info").mkdir(exist_ok=True)
    return detect.find_foreign(environ=environ, sys_path=[str(site)], argv=["python", "app.py"], own_roots=[])


def test_default_tracing_is_no_finding(tmp_path):
    assert find(tmp_path, TRACING) == []
    assert find(tmp_path, dict(TRACING, LANGSMITH_TRACING_MODE="langsmith")) == []
    assert find(tmp_path, dict(TRACING, LANGSMITH_OTEL_ENABLED="false")) == []


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("LANGSMITH_OTEL_ENABLED", "true"),
        ("LANGSMITH_OTEL_ENABLED", "True"),
        ("LANGSMITH_OTEL_ENABLED", "1"),
        ("LANGSMITH_OTEL_ONLY", "true"),
        ("LANGCHAIN_OTEL_ENABLED", "true"),
        ("LANGSMITH_TRACING_MODE", "otel"),
        ("LANGSMITH_TRACING_MODE", "hybrid"),
        ("LANGSMITH_TRACING_MODE", "OTEL"),
        ("LANGCHAIN_TRACING_MODE", "hybrid"),
    ],
)
def test_the_opentelemetry_mode_is_the_workloads_own_tracing(tmp_path, setting, value):
    findings = find(tmp_path, dict(TRACING, **{setting: value}))
    assert [(f.kind, f.name) for f in findings] == [("environment", setting)]
    # The detail names the setting, never its value.
    assert detect.describe(findings) == f"{setting} (set by the workload)"


def test_the_mode_setting_decides_before_the_switches(tmp_path):
    assert find(tmp_path, dict(TRACING, LANGSMITH_TRACING_MODE="langsmith", LANGSMITH_OTEL_ENABLED="true")) == []
    found = find(tmp_path, dict(TRACING, LANGSMITH_TRACING_MODE="otel", LANGSMITH_OTEL_ENABLED="false"))
    assert [f.name for f in found] == ["LANGSMITH_TRACING_MODE"]
    # A blank mode is not set: the switches decide.
    found = find(tmp_path, dict(TRACING, LANGSMITH_TRACING_MODE=" ", LANGSMITH_OTEL_ENABLED="true"))
    assert [f.name for f in found] == ["LANGSMITH_OTEL_ENABLED"]


def test_langsmith_prefixed_settings_come_before_langchain_prefixed_ones(tmp_path):
    assert find(tmp_path, dict(TRACING, LANGSMITH_TRACING_MODE="langsmith", LANGCHAIN_TRACING_MODE="otel")) == []
    # LANGSMITH_OTEL_ENABLED is set, so LANGCHAIN_OTEL_ENABLED is not read.
    assert find(tmp_path, dict(TRACING, LANGSMITH_OTEL_ENABLED="false", LANGCHAIN_OTEL_ENABLED="true")) == []


def test_the_mode_is_listed_after_the_tracers_found(tmp_path):
    site = tmp_path / "site2"
    site.mkdir()
    (site / "ddtrace-4.16.1.dist-info").mkdir()
    found = detect.find_foreign(
        environ=dict(TRACING, LANGSMITH_OTEL_ENABLED="true"), sys_path=[str(site)], argv=["python"], own_roots=[]
    )
    assert [f.name for f in found] == ["ddtrace", "LANGSMITH_OTEL_ENABLED"]


# --- the tool the setup report names ---------------------------------------------------------------------------------


def tool_of(tmp_path, environ, *dists):
    site = tmp_path / "named"
    site.mkdir(exist_ok=True)
    for dist in dists or ("langsmith-0.14.17", "langchain_core-1.6.13", "opentelemetry_sdk-1.45.2"):
        (site / f"{dist}.dist-info").mkdir(exist_ok=True)
    scan = detect.scan([str(site)], own_roots=[])
    findings = detect.find_foreign(environ=environ, sys_path=[], argv=["python", "app.py"], own_roots=[], scan=scan)
    return detect.tool(findings, scan.dists, environ)


@pytest.mark.parametrize(
    "tracing",
    [
        {"LANGSMITH_TRACING": "true"},
        {"LANGCHAIN_TRACING_V2": "true"},
        {"LANGSMITH_TRACING_V2": "true", "LANGSMITH_TRACING": "false"},
    ],
)
def test_default_tracing_names_langsmith_with_its_version_and_no_exporter(tmp_path, tracing):
    assert tool_of(tmp_path, tracing) == detect.Tool("langsmith", "0.14.17", None)


@pytest.mark.parametrize(
    "environ",
    [
        {},  # every LangChain app has the package: it alone is no tool
        {"LANGSMITH_API_KEY": "lsv2_pt_planted"},
        {"LANGSMITH_TRACING": "false"},
        {"LANGSMITH_TRACING_V2": "false", "LANGSMITH_TRACING": "true"},  # TRACING_V2 decides when it is set
        {"LANGSMITH_TRACING": "True"},  # the SDK compares with "true" exactly
    ],
)
def test_without_tracing_switched_on_langsmith_is_no_tool(tmp_path, environ):
    assert tool_of(tmp_path, environ) is None


def test_tracing_switched_on_without_the_package_is_no_tool(tmp_path):
    assert tool_of(tmp_path, TRACING, "openai-3.42.1") is None


@pytest.mark.parametrize(
    ("setting", "value"),
    [("LANGSMITH_OTEL_ENABLED", "true"), ("LANGSMITH_OTEL_ONLY", "true"), ("LANGSMITH_TRACING_MODE", "otel")],
)
def test_the_opentelemetry_mode_names_langsmith_exporting_otlp_over_http(tmp_path, setting, value):
    # Whatever OTLP protocol the environment names: LangSmith's exporter is its own, OTLP over HTTP.
    environ = dict(TRACING, OTEL_EXPORTER_OTLP_PROTOCOL="grpc", **{setting: value})
    assert tool_of(tmp_path, environ) == detect.Tool("langsmith", "0.14.17", "otlp-http")


@pytest.mark.parametrize("tracer", ["agentops-0.4.21", "openinference_instrumentation_openai-0.1.30"])
def test_langsmith_tracing_beside_a_tracer_of_no_known_tool_names_no_tool(tmp_path, tracer):
    # The unknown tracer made the package stand down; LangSmith's default tracing beside it does not make LangSmith the
    # owner, so the report names no tool and impact.ai reads "owned by another tracer".
    assert tool_of(tmp_path, TRACING, "langsmith-0.14.17", "langchain_core-1.6.13", tracer) is None


@pytest.mark.parametrize("setting", ["LANGSMITH_OTEL_ENABLED", "LANGCHAIN_OTEL_ONLY", "LANGSMITH_TRACING_MODE"])
def test_a_langsmith_setting_without_the_package_names_no_tool(tmp_path, setting):
    environ = {setting: "otel" if setting.endswith("MODE") else "true"}
    assert tool_of(tmp_path, environ, "openai-3.42.1", "opentelemetry_sdk-1.45.2") is None

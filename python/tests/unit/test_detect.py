# SPDX-License-Identifier: Apache-2.0

"""Stand down: the package stands down when the workload already has a tracer.

Detection must use the standard library only and must never import a candidate package, because it runs from
sitecustomize before the application starts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from impact_instrumentation import detect

KIT_ENV = {"IMPACT_OTLP_ENDPOINT": "http://gateway.impact-system.svc:4318"}


def site(tmp_path: Path, name: str, *dists: str, modules: tuple[str, ...] = ()) -> Path:
    """A fake site-packages directory holding the given dist-info directories and top-level modules."""
    root = tmp_path / name
    root.mkdir()
    for dist in dists:
        (root / f"{dist}.dist-info").mkdir()
    for module in modules:
        (root / module).mkdir()
        (root / module / "__init__.py").write_text("")
    return root


def find(tmp_path: Path, *paths: Path, environ=None, argv=("python", "app.py"), orig_argv=None, own=()):
    return detect.find_foreign(
        environ=environ if environ is not None else {},
        sys_path=[str(p) for p in paths],
        argv=list(argv),
        orig_argv=list(orig_argv) if orig_argv else None,
        own_roots=[str(p) for p in own],
    )


def labels(findings) -> list[str]:
    return [f.name for f in findings]


def test_clean_environment_finds_nothing(tmp_path):
    app = site(tmp_path, "app", "fastapi-0.115.0", "openai-1.50.0", "uvicorn-0.30.0")
    assert find(tmp_path, app) == []


def test_only_impact_ais_sdk_is_a_tracer_not_the_kits_own_package(tmp_path):
    # "impact" is the distribution of Impact AI's SDK; the kit's own package, impact-instrumentation, is not it.
    app = site(tmp_path, "app", "fastapi-0.115.0", "impact_instrumentation-0.3.0")
    assert find(tmp_path, app) == []


@pytest.mark.parametrize(
    ("dist", "name", "version"),
    [
        ("ddtrace-2.9.1", "ddtrace", "2.9.1"),
        ("traceloop_sdk-0.46.0", "traceloop-sdk", "0.46.0"),
        ("langfuse-3.2.1", "langfuse", "3.2.1"),
        ("openlit-1.34.0", "openlit", "1.34.0"),
        ("logfire-4.0.0", "logfire", "4.0.0"),
        ("newrelic-10.1.0", "newrelic", "10.1.0"),
        ("opentelemetry_instrumentation_openai-0.62.4", "opentelemetry-instrumentation-openai", "0.62.4"),
        ("opentelemetry_instrumentation_bedrock-0.62.4", "opentelemetry-instrumentation-bedrock", "0.62.4"),
        ("openinference_instrumentation_openai-0.1.30", "openinference-instrumentation-openai", "0.1.30"),
        ("elastic_opentelemetry-1.19.0", "elastic-opentelemetry", "1.19.0"),
        ("arize_otel-0.14.1", "arize-otel", "0.14.1"),
        ("arize_phoenix_otel-0.17.3", "arize-phoenix-otel", "0.17.3"),
        ("impact-1.2.0rc8", "impact", "1.2.0rc8"),
    ],
)
def test_installed_tracers_are_found_by_distribution_name(tmp_path, dist, name, version):
    app = site(tmp_path, "app", "fastapi-0.115.0", dist)
    found = find(tmp_path, app)
    assert len(found) == 1
    assert found[0].name == name
    assert found[0].version == version
    assert found[0].describe() == f"{name} {version}"


def test_contrib_genai_packages_are_not_other_tracers(tmp_path):
    # These are the names this image ships itself; an app that depends on them directly is not running a tracer.
    app = site(
        tmp_path,
        "app",
        "opentelemetry_instrumentation_openai_v2-2.0b0",
        "opentelemetry_instrumentation_genai_openai-1.1b0",
        "opentelemetry_api-1.45.2",
    )
    assert find(tmp_path, app) == []


def test_egg_info_installs_are_found(tmp_path):
    app = tmp_path / "legacy"
    app.mkdir()
    (app / "ddtrace-1.20.0-py3.11.egg-info").mkdir()
    found = find(tmp_path, app)
    assert labels(found) == ["ddtrace"]
    assert found[0].version == "1.20.0"


def test_vendored_module_without_metadata_is_found(tmp_path):
    # Datadog's own injector puts ddtrace on PYTHONPATH from /datadog-lib without any dist-info.
    lib = site(tmp_path, "datadog-lib", modules=("ddtrace",))
    found = find(tmp_path, lib)
    assert labels(found) == ["ddtrace"]
    assert found[0].version is None
    assert found[0].describe() == "ddtrace"


def test_our_own_payload_is_never_a_finding(tmp_path):
    payload = site(tmp_path, "otel-auto-instrumentation-python", "opentelemetry_instrumentation_bedrock-0.62.4")
    app = site(tmp_path, "app", "fastapi-0.115.0")
    assert find(tmp_path, payload, app, own=(payload,)) == []


def test_the_same_package_in_the_app_environment_is_a_finding(tmp_path):
    payload = site(tmp_path, "payload", "opentelemetry_instrumentation_bedrock-0.62.4")
    app = site(tmp_path, "app", "opentelemetry_instrumentation_bedrock-0.60.0")
    found = find(tmp_path, payload, app, own=(payload,))
    assert [(f.name, f.version) for f in found] == [("opentelemetry-instrumentation-bedrock", "0.60.0")]


@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/local/bin/opentelemetry-instrument", "gunicorn", "app:app"],
        ["/venv/bin/ddtrace-run", "uvicorn", "app:app"],
        ["/venv/bin/newrelic-admin", "run-program", "python", "app.py"],
    ],
)
def test_app_owned_launchers_are_found(tmp_path, argv):
    found = find(tmp_path, site(tmp_path, "app"), argv=argv)
    assert len(found) == 1
    assert found[0].kind == "launcher"


def test_launcher_is_found_when_started_as_a_module(tmp_path):
    found = find(
        tmp_path,
        site(tmp_path, "app"),
        argv=["/usr/lib/python3.12/ddtrace/commands/ddtrace_run.py", "uvicorn"],
        orig_argv=["python", "-m", "ddtrace.commands.ddtrace_run", "uvicorn"],
    )
    assert [(f.kind, f.name) for f in found] == [("launcher", "ddtrace-run")]


def test_ordinary_server_command_is_not_a_launcher(tmp_path):
    app = site(tmp_path, "app", "uvicorn-0.30.0")
    assert find(tmp_path, app, argv=["/venv/bin/uvicorn", "app:app", "--port", "8000"]) == []
    assert find(tmp_path, app, argv=["/venv/bin/gunicorn", "app:app"]) == []


def test_foreign_exporter_settings_count_only_with_an_otel_sdk_in_the_app(tmp_path):
    env = dict(KIT_ENV, OTEL_EXPORTER_OTLP_ENDPOINT="http://collector.observability:4318")
    without_sdk = site(tmp_path, "plain", "fastapi-0.115.0")
    with_sdk = site(tmp_path, "otel", "fastapi-0.115.0", "opentelemetry_sdk-1.30.0")
    assert find(tmp_path, without_sdk, environ=env) == []
    found = find(tmp_path, with_sdk, environ=env)
    assert labels(found) == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert found[0].kind == "environment"


@pytest.mark.parametrize(
    "name",
    [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_ZIPKIN_ENDPOINT",
        "OTEL_EXPORTER_JAEGER_ENDPOINT",
    ],
)
def test_each_exporter_setting_is_an_ownership_signal(tmp_path, name):
    with_sdk = site(tmp_path, "otel", "opentelemetry_sdk-1.30.0")
    assert labels(find(tmp_path, with_sdk, environ=dict(KIT_ENV, **{name: "x"}))) == [name]


def test_the_variables_the_operator_sets_are_not_ownership_signals(tmp_path):
    env = dict(
        KIT_ENV,
        OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf",
        OTEL_TRACES_EXPORTER="otlp",
        OTEL_METRICS_EXPORTER="otlp",
        OTEL_LOGS_EXPORTER="otlp",
        OTEL_SERVICE_NAME="support-assistant",
        OTEL_RESOURCE_ATTRIBUTES="k8s.namespace.name=shop",
    )
    with_sdk = site(tmp_path, "otel", "opentelemetry_sdk-1.30.0")
    assert find(tmp_path, with_sdk, environ=env) == []


def test_another_traces_exporter_choice_is_an_ownership_signal(tmp_path):
    app = site(tmp_path, "app")
    assert labels(find(tmp_path, app, environ={"OTEL_TRACES_EXPORTER": "console"})) == ["OTEL_TRACES_EXPORTER"]
    assert find(tmp_path, app, environ={"OTEL_TRACES_EXPORTER": "otlp"}) == []


def test_without_the_kit_endpoint_the_exporter_settings_belong_to_us(tmp_path):
    # Outside the cluster kit (a launcher on a VM, say) OTEL_EXPORTER_OTLP_* is how the package is pointed at ingest.
    with_sdk = site(tmp_path, "otel", "opentelemetry_sdk-1.30.0")
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://ingest.impact.ai"}
    assert find(tmp_path, with_sdk, environ=env) == []


def test_findings_are_ordered_and_described_for_the_setup_span(tmp_path):
    app = site(tmp_path, "app", "ddtrace-2.9.1", "langfuse-3.2.1")
    found = find(tmp_path, app, argv=["/venv/bin/opentelemetry-instrument", "python", "app.py"])
    assert detect.describe(found) == "opentelemetry-instrument (launcher), ddtrace 2.9.1, langfuse 3.2.1"


def test_the_tracer_leads_the_description_not_the_provider_packages_it_brings(tmp_path):
    # Traceloop installs a dozen per-provider packages that sort before its own name; the description is cut at
    # MAX_DETAIL, so the tracer must come first or it would be cut away.
    providers = [f"opentelemetry_instrumentation_{p}-0.62.4" for p in ("alephalpha", "anthropic", "bedrock", "cohere")]
    providers += [f"openinference_instrumentation_{p}-0.1.0" for p in ("openai", "bedrock")]
    app = site(tmp_path, "app", "traceloop_sdk-0.62.4", *providers)
    detail = detect.describe(find(tmp_path, app, argv=["/venv/bin/opentelemetry-instrument", "python", "app.py"]))
    assert detail.startswith("opentelemetry-instrument (launcher), traceloop-sdk 0.62.4, ")
    assert "opentelemetry-instrumentation-alephalpha 0.62.4" in detail
    only = detect.describe(find(tmp_path, app))
    assert only.startswith("traceloop-sdk 0.62.4, ")
    assert only.index("traceloop-sdk") < only.index("opentelemetry-instrumentation-alephalpha")


def test_description_is_bounded():
    findings = [detect.Finding(kind="distribution", name=f"ddtrace-{n}", version="1.0") for n in range(100)]
    assert len(detect.describe(findings)) <= detect.MAX_DETAIL


def test_unreadable_or_odd_sys_path_entries_are_ignored(tmp_path):
    file_entry = tmp_path / "archive.zip"
    file_entry.write_text("not a directory")
    app = site(tmp_path, "app", "ddtrace-2.9.1")
    found = detect.find_foreign(
        environ={},
        sys_path=["", str(tmp_path / "missing"), str(file_entry), str(app)],
        argv=["python"],
        own_roots=[],
    )
    assert labels(found) == ["ddtrace"]


def test_scan_lists_installed_distributions_with_versions(tmp_path):
    app = site(tmp_path, "app", "openai-1.50.0", "fastapi-0.115.0", "Typing_Extensions-4.9.0", "ruamel.yaml-0.18.6")
    scan = detect.scan([str(app)], own_roots=[])
    assert scan.dists == {
        "openai": "1.50.0",
        "fastapi": "0.115.0",
        "typing-extensions": "4.9.0",
        "ruamel-yaml": "0.18.6",
    }


def test_scan_skips_our_own_roots(tmp_path):
    payload = site(tmp_path, "payload", "openai-9.9.9")
    app = site(tmp_path, "app", "fastapi-0.115.0")
    scan = detect.scan([str(payload), str(app)], own_roots=[str(payload)])
    assert scan.dists == {"fastapi": "0.115.0"}


def test_scan_takes_the_first_occurrence_as_the_import_system_would(tmp_path):
    first = site(tmp_path, "first", "openai-1.0.0")
    second = site(tmp_path, "second", "openai-2.0.0")
    assert detect.scan([str(first), str(second)], own_roots=[]).dists == {"openai": "1.0.0"}


def test_find_foreign_can_reuse_a_scan(tmp_path):
    app = site(tmp_path, "app", "ddtrace-2.9.1")
    scan = detect.scan([str(app)], own_roots=[])
    found = detect.find_foreign(environ={}, sys_path=[], argv=["python"], own_roots=[], scan=scan)
    assert [f.name for f in found] == ["ddtrace"]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("Typing_Extensions", "typing-extensions"), ("ruamel.yaml", "ruamel-yaml"), ("a__b--c..d", "a-b-c-d"), ("X", "x")],
)
def test_normalize_follows_pep_503(raw, normalized):
    assert detect.normalize(raw) == normalized


# --- the tool the setup report names: the tool beside the package ----------------------------------------------------


def named(tmp_path: Path, *dists: str, environ=None, argv=("python", "app.py"), modules: tuple[str, ...] = ()):
    root = site(tmp_path, "named", *dists, modules=modules)
    env = environ if environ is not None else {}
    scan = detect.scan([str(root)], own_roots=[])
    findings = detect.find_foreign(environ=env, sys_path=[], argv=list(argv), own_roots=[], scan=scan)
    return detect.tool(findings, scan.dists, env)


@pytest.mark.parametrize(
    ("dists", "tool"),
    [
        (("ddtrace-4.16.1",), ("datadog", "4.16.1", None)),
        (("traceloop_sdk-0.46.0", "opentelemetry_instrumentation_openai-0.62.4"), ("traceloop", "0.46.0", None)),
        (("langfuse-3.2.1",), ("langfuse", "3.2.1", None)),
        (("openlit-1.34.0",), ("openlit", "1.34.0", None)),
        (("logfire-4.3.0",), ("logfire", "4.3.0", None)),
        (("arize_phoenix_otel-0.13.0",), ("phoenix", "0.13.0", None)),
        (("arize_otel-0.10.0",), ("arize-ax", "0.10.0", None)),
        (("newrelic-11.0.1",), ("newrelic", "11.0.1", None)),
        (("splunk_opentelemetry-2.7.0",), ("splunk-otel", "2.7.0", None)),
        (("honeycomb_opentelemetry-0.6.0",), ("honeycomb", "0.6.0", None)),
        (("elastic_opentelemetry-1.4.0",), ("elastic-edot", "1.4.0", None)),
    ],
)
def test_a_tracer_names_its_tool_by_the_span_source_id_and_its_version(tmp_path, dists, tool):
    assert named(tmp_path, *dists) == detect.Tool(*tool)


@pytest.mark.parametrize(
    ("argv", "dists", "tool"),
    [
        (("ddtrace-run", "python", "app.py"), ("ddtrace-4.16.1",), ("datadog", "4.16.1", None)),
        (("newrelic-admin", "run-program"), (), ("newrelic", None, None)),
        (("splunk-py-trace", "python"), (), ("splunk-otel", None, None)),
        (("opentelemetry-instrument", "uvicorn"), ("opentelemetry_sdk-1.45.2",), ("opentelemetry", "1.45.2", None)),
    ],
)
def test_a_launcher_names_its_tool(tmp_path, argv, dists, tool):
    assert named(tmp_path, *dists, argv=argv) == detect.Tool(*tool)


def test_a_tracer_found_by_its_module_alone_names_its_tool_without_a_version(tmp_path):
    assert named(tmp_path, modules=("ddtrace",)) == detect.Tool("datadog")


@pytest.mark.parametrize(
    "dists",
    [
        ("agentops-0.4.21",),
        ("opentelemetry_instrumentation_openai-0.62.4",),  # OpenLLMetry's instrumentation alone: several tools start it
        ("openinference_instrumentation_openai-0.1.30",),
        ("elastic_apm-6.24.0",),
    ],
)
def test_a_tracer_of_no_known_tool_names_none(tmp_path, dists):
    assert named(tmp_path, *dists) is None


def test_no_finding_names_no_tool(tmp_path):
    assert named(tmp_path, "openai-3.42.1", "langsmith-0.14.17") is None


@pytest.mark.parametrize(
    ("environ", "exporter"),
    [
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"}, "otlp-http"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}, "otlp-http"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "otlp-grpc"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf", "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc"}, "otlp-grpc"),
        ({"OTEL_TRACES_EXPORTER": "zipkin", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "zipkin"),
        ({"OTEL_TRACES_EXPORTER": "Console"}, "console"),
        ({"OTEL_TRACES_EXPORTER": "otlp", "OTEL_EXPORTER_ZIPKIN_ENDPOINT": "http://zipkin:9411"}, "zipkin"),
        ({}, None),  # set up in code, or the SDK's default: not shown by the settings
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "carrier-pigeon"}, None),
        ({"OTEL_TRACES_EXPORTER": "none"}, None),
    ],
)
def test_an_opentelemetry_pipeline_names_its_exporter_kind_and_never_a_value(tmp_path, environ, exporter):
    tool = named(tmp_path, argv=("opentelemetry-instrument", "uvicorn"), environ=environ)
    assert tool == detect.Tool("opentelemetry", None, exporter)


def test_the_workloads_own_pipeline_names_opentelemetry(tmp_path):
    env = {**KIT_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}
    tool = named(tmp_path, "opentelemetry_sdk-1.45.2", environ=env)
    assert tool == detect.Tool("opentelemetry", "1.45.2", "otlp-grpc")


def test_the_first_finding_that_shows_a_tool_names_it(tmp_path):
    env = {**KIT_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}
    tool = named(tmp_path, "agentops-0.4.21", "ddtrace-4.16.1", "opentelemetry_sdk-1.45.2", environ=env)
    assert tool == detect.Tool("datadog", "4.16.1")


@pytest.mark.parametrize("version", ["1.0 beta", "x" * 65, "-1", "1.0\n", "é1"])
def test_a_version_impact_ai_would_not_keep_is_left_out(tmp_path, version):
    findings = [detect.Finding("distribution", "openlit", version)]
    assert detect.tool(findings, {"openlit": version}, {}) == detect.Tool("openlit")

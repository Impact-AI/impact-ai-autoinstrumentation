# SPDX-License-Identifier: Apache-2.0

"""Stand-down parity with the Python package: the workload starts a tracer of its own and is annotated anyway.
The package does not start, the workload's own spans reach its own endpoint as before, and the setup report says what
was found."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from containers import http, wait_until
from nodejs_support import app_image, ask, only, readable, require_images, setup_reports, wait_ready

NODE = "22"
TOOL_PREFIXES = ("impact.setup.tool", "impact.setup.exporter", "impact.setup.alongside")
OPENAI_APP = ["node", "openai.cjs"]
DIRECTORY = "/apps/openai-7"


def stub_package(root: Path, name: str, version: str, *files: str) -> str:
    """A package directory with a manifest (and empty files), to mount into the application's node_modules."""
    package = root / name.replace("/", "__")
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": name, "version": version}))
    for file in files:
        (package / file).write_text("")
    readable(package)
    return str(package)


def mount(source: str, name: str) -> dict[str, str]:
    return {source: f"{DIRECTORY}/node_modules/{name}:ro"}


def assert_stood_down(app, mock, detail_starts_with: str) -> dict:
    wait_until(lambda: setup_reports(mock), timeout=30, message=lambda: f"the setup report\n{app.logs()}")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "pre-existing"
    assert report["attrs"]["impact.setup.detail"].startswith(detail_starts_with), report["attrs"]
    # a stood-down workload still says which AI libraries it has: Cover your AI shows them next to who owns the tracing
    assert report["attrs"]["impact.setup.frameworks"] == ["openai 7.50.0"]
    assert report["attrs"]["impact.setup.untraced"] == []
    # nothing but the report reaches impact.ai
    assert [s["name"] for s in mock.spans()] == ["impact.setup"]
    return report


@pytest.mark.nodejs
@pytest.mark.parametrize(
    ("package", "version", "detail", "tool"),
    [
        ("dd-trace", "5.60.0", "dd-trace 5.60.0", "datadog"),
        ("@traceloop/node-server-sdk", "0.19.0", "@traceloop/node-server-sdk 0.19.0", "traceloop"),
        ("langfuse", "3.38.0", "langfuse 3.38.0", "langfuse"),
        (
            "@arizeai/openinference-instrumentation-openai",
            "4.4.2",
            "@arizeai/openinference-instrumentation-openai 4.4.2",
            None,  # several tools start OpenInference's instrumentations: the report names none
        ),
    ],
)
def test_a_tracer_installed_in_the_application_stands_the_package_down(
    start_app, mock, tmp_path, package, version, detail, tool
):
    require_images(app_image(NODE))
    app = start_app(
        NODE, OPENAI_APP, directory=DIRECTORY, mounts=mount(stub_package(tmp_path, package, version), package)
    )
    wait_ready(app)
    assert ask(app, "openai") == (200, {"answer": "Hello from the stub."})
    report = assert_stood_down(app, mock, detail)
    # it names the tool it found, its version, and that it did not start beside it
    named = {key: value for key, value in report["attrs"].items() if key.startswith(TOOL_PREFIXES)}
    expected = {"impact.setup.tool": tool, "impact.setup.tool.version": version, "impact.setup.alongside": False}
    assert named == (expected if tool else {})
    # we left no trace in the process: no environment default, and the application's NODE_OPTIONS is as the Operator
    # made it
    runtime = http("GET", f"{app.url}/debug/runtime")[1]
    assert not [
        name
        for name in runtime["environment"]
        if name in ("OTEL_LOGS_EXPORTER", "OTEL_METRICS_EXPORTER") or name.startswith("ANTHROPIC_")
    ]


@pytest.mark.nodejs
def test_a_preload_of_a_tracer_started_with_node_options_stands_the_package_down(start_app, mock, tmp_path):
    """`NODE_OPTIONS=--require dd-trace/init` (Datadog's injector does this): found by the name, before the tracer
    runs."""
    require_images(app_image(NODE))
    init = stub_package(tmp_path, "dd-trace", "5.60.0", "init.js")
    app = start_app(
        NODE, OPENAI_APP, directory=DIRECTORY, mounts=mount(init, "dd-trace"), node_options="--require dd-trace/init"
    )
    wait_ready(app)
    assert ask(app, "openai")[0] == 200
    report = assert_stood_down(app, mock, "dd-trace/init (preload)")
    assert report["attrs"]["impact.setup.tool"] == "datadog"
    assert report["attrs"]["impact.setup.alongside"] is False


@pytest.mark.nodejs
def test_a_tracer_provider_registered_by_an_earlier_preload_stands_the_package_down(start_app, mock, tmp_path):
    earlier = tmp_path / "earlier.cjs"
    earlier.write_text("globalThis[Symbol.for('opentelemetry.js.api.1')] = { version: '1.10.0', trace: {} };\n")
    readable(earlier)
    require_images(app_image(NODE))
    app = start_app(
        NODE,
        OPENAI_APP,
        directory=DIRECTORY,
        mounts={str(earlier): "/work/earlier.cjs:ro"},
        node_options="--require /work/earlier.cjs",
    )
    wait_ready(app)
    assert ask(app, "openai")[0] == 200
    report = assert_stood_down(app, mock, "an OpenTelemetry tracer provider (registered before this package)")
    assert report["attrs"]["impact.setup.tool"] == "opentelemetry"


@pytest.mark.nodejs
def test_a_tracer_package_that_an_earlier_preload_loaded_stands_the_package_down(start_app, mock, tmp_path):
    init = stub_package(tmp_path, "newrelic", "12.5.0", "index.js")
    (Path(init) / "index.js").write_text("")  # loaded, as an agent started from NODE_OPTIONS=-r newrelic is
    app = start_app(NODE, OPENAI_APP, directory=DIRECTORY, mounts=mount(init, "newrelic"), node_options="-r newrelic")
    wait_ready(app)
    report = assert_stood_down(app, mock, "newrelic")
    assert report["attrs"]["impact.setup.tool"] == "newrelic"
    assert report["attrs"]["impact.setup.tool.version"] == "12.5.0"


@pytest.mark.nodejs
def test_the_opt_out_starts_nothing_and_reports_nothing(start_app, mock):
    require_images(app_image(NODE))
    app = start_app(NODE, OPENAI_APP, directory=DIRECTORY, env={"OTEL_SDK_DISABLED": "true"})
    wait_ready(app)
    assert ask(app, "openai")[0] == 200
    import time

    time.sleep(1.5)
    assert mock.spans() == []


@pytest.mark.nodejs
def test_an_opentelemetry_pipeline_started_in_code_keeps_its_spans_and_its_endpoint(start_app, mock, theirs):
    """The workload runs its own OpenTelemetry SDK with its own exporter. Its server span reaches its own backend as
    before."""
    require_images(app_image(NODE))
    app = start_app(
        NODE,
        ["node", "own-otel.cjs"],
        directory="/apps/own-otel",
        env={
            "OTEL_EXPORTER_OTLP_ENDPOINT": theirs.internal_url,
            "OTEL_SERVICE_NAME": "theirs",
            "THEIRS_URL": theirs.internal_url,
        },
    )
    wait_ready(app)
    assert ask(app, "openai")[0] == 200
    assert_stood_down_for_pipeline(app, mock)
    wait_until(
        lambda: any(s["name"] == "POST" for s in theirs.spans()),
        timeout=30,
        message=lambda: f"the workload's own server span among {[s['name'] for s in theirs.spans()]}\n{app.logs()}",
    )
    own = [s for s in theirs.spans() if s["name"] == "POST"]
    assert own and own[0]["resource"]["service.name"] == "theirs"


def assert_stood_down_for_pipeline(app, mock):
    wait_until(lambda: setup_reports(mock), timeout=30, message=lambda: f"the setup report\n{app.logs()}")
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "pre-existing"
    assert report["attrs"]["impact.setup.detail"].startswith("OTEL_EXPORTER_OTLP_ENDPOINT")
    assert [s["name"] for s in mock.spans()] == ["impact.setup"]


@pytest.mark.nodejs
def test_the_package_is_not_in_the_way_of_the_applications_own_tracing_when_it_stands_down(start_app, mock, tmp_path):
    """Neither our provider nor our context manager is registered in a process that stood down."""
    require_images(app_image(NODE))
    probe = tmp_path / "probe.cjs"
    probe.write_text(
        "const api=globalThis[Symbol.for('opentelemetry.js.api.1')];"
        "const body=JSON.stringify({api:Boolean(api&&(api.trace||api.context))});"
        "require('http').createServer((q,r)=>r.end(body)).listen(8000);"
    )
    readable(probe)
    stubs = stub_package(tmp_path, "dd-trace", "5.60.0")
    app = start_app(
        NODE,
        ["node", "/work/probe.cjs"],
        directory=DIRECTORY,
        mounts={str(probe): "/work/probe.cjs:ro", stubs: f"{DIRECTORY}/node_modules/dd-trace:ro"},
    )
    wait_until(lambda: http("GET", f"{app.url}/")[0] == 200, timeout=60, message=lambda: app.logs())
    assert http("GET", f"{app.url}/")[1] == {"api": False}

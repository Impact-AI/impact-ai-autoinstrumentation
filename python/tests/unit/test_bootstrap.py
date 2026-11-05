# SPDX-License-Identifier: Apache-2.0

"""Process start, end to end in a real interpreter: the sitecustomize the Operator's PYTHONPATH makes Python load.

A fake payload root stands in for /otel-auto-instrumentation-python: our package, our sitecustomize in the Operator
layout, and fake instrumentors that record what they were given. The instrumentation is lazy: it is armed at start and
applied when the application imports the library, so a process that never does pays almost nothing.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import Sink
from impact_instrumentation import defaults

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import assemble_payload as assemble  # noqa: E402

SRC = ROOT / "src"

FAKE_INSTRUMENTOR = """
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

class {cls}(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        provider = kwargs["tracer_provider"]
        with provider.get_tracer("{name}").start_as_current_span("{name}.instrumented"):
            pass

    def _uninstrument(self, **kwargs):
        pass
"""


class Layout:
    """/otel-auto-instrumentation-python, the application's site-packages and its own PYTHONPATH entry."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "otel-auto-instrumentation-python"
        self.app_site = tmp_path / "app-site"
        self.app_path = tmp_path / "app-code"
        self.state = tmp_path / "state"
        self.prefix = self.root / "opentelemetry" / "instrumentation" / "auto_instrumentation"
        self.prefix.mkdir(parents=True)
        for directory in (self.app_site, self.app_path, self.state):
            directory.mkdir()
        shutil.copytree(SRC / "impact_instrumentation", self.root / "impact_instrumentation")
        shutil.copy(SRC / "sitecustomize.py", self.prefix / "sitecustomize.py")
        (self.root / "_impact_shared").mkdir()
        self.add_library("fakelib")
        self.add_instrumentor("fake", "FakeInstrumentor", library="fakelib")

    def add_library(self, name: str, version: str = "1.0") -> None:
        """A library of the application: an importable package and its distribution metadata."""
        (self.app_site / name).mkdir(exist_ok=True)
        (self.app_site / name / "__init__.py").write_text(f"VERSION = {version!r}\n")
        info = self.app_site / f"{name}-{version}.dist-info"
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")

    def add_instrumentor(self, name: str, cls: str, *, library: str, constraint: str = "", where: Path | None = None):
        where = where or self.root
        (where / f"{name}_instr").mkdir()
        (where / f"{name}_instr" / "__init__.py").write_text(FAKE_INSTRUMENTOR.format(cls=cls, name=name))
        info = where / f"opentelemetry_instrumentation_{name}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\n"
            f"Name: opentelemetry-instrumentation-{name}\nVersion: 1.0\n"
            f'Requires-Dist: {library}{constraint}; extra == "instruments"\n'
        )
        (info / "entry_points.txt").write_text(f"[opentelemetry_instrumentor]\n{name} = {name}_instr:{cls}\n")

    def add_installed(self, dist: str) -> None:
        (self.app_site / f"{dist}.dist-info").mkdir()

    @property
    def app_pythonpath(self) -> str:
        """The PYTHONPATH the workload sets itself (its code, and its dependencies as the fake site-packages)."""
        return os.pathsep.join([str(self.app_path), str(self.app_site)])

    def pythonpath(self) -> str:
        """What the Operator builds: <prefix>:<the application's own PYTHONPATH>:<root>."""
        return os.pathsep.join([str(self.prefix), self.app_pythonpath, str(self.root)])

    def env(self, sink: Sink | None, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": self.pythonpath(),
            "OTEL_SERVICE_NAME": "support-assistant",
            "OTEL_RESOURCE_ATTRIBUTES": "k8s.namespace.name=shop,k8s.deployment.name=support-assistant",
            "OTEL_BSP_SCHEDULE_DELAY": "100",
            "IMPACT_STATE_DIR": str(self.state),
        }
        if sink is not None:
            env["IMPACT_OTLP_ENDPOINT"] = sink.url
        env.update(extra)
        return env

    def run(self, code: str, env: dict[str, str], timeout: float = 60) -> subprocess.CompletedProcess:
        # The registry is generated when the payload is built; do the same for the fake payload.
        (self.root / "impact_instrumentation" / "_registry.py").write_text(assemble.generate_registry(self.root))
        return subprocess.run(  # noqa: S603 - fixed interpreter, test-owned code
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=timeout, check=False
        )


@pytest.fixture
def layout(tmp_path):
    return Layout(tmp_path)


@pytest.fixture
def sink():
    s = Sink()
    yield s
    s.close()


STATE = """
import json, os, sys
print(json.dumps({
    "path": sys.path,
    "pythonpath": os.environ.get("PYTHONPATH"),
    "loaded": sorted(m for m in sys.modules if m.split(".")[0] in {"opentelemetry", "fakelib", "wrapt", "packaging"}),
    "stdlib_http": "http.client" in sys.modules,
}))
"""
USE_LIBRARY = "import fakelib\n" + STATE


def decoded(value: dict):
    """A string, or the list of strings of an array attribute."""
    if "arrayValue" in value:
        return [item["stringValue"] for item in value["arrayValue"].get("values", [])]
    return value["stringValue"]


def setup_reports(sink: Sink) -> list[dict]:
    """The attributes of every impact.setup span, in arrival order."""
    return [
        {a["key"]: decoded(a["value"]) for a in span["attributes"]}
        for span in sink.spans()
        if span["name"] == "impact.setup"
    ]


def setup_attributes(sink: Sink) -> dict:
    reports = setup_reports(sink)
    assert reports, f"no impact.setup span among {[s['name'] for s in sink.spans()]}"
    return reports[0]


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def has_span(sink: Sink, name: str) -> bool:
    return any(s["name"] == name for s in sink.spans())


def parse(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def otel_modules(got: dict) -> list[str]:
    return [m for m in got["loaded"] if m.startswith("opentelemetry")]


# --- start: armed, not applied -------------------------------------------------------------------------------------


def test_a_clean_start_arms_the_instrumentation_and_imports_nothing(layout, sink):
    got = parse(layout.run(STATE, layout.env(sink)))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    attrs = setup_attributes(sink)
    assert attrs["impact.setup.state"] == "active"
    assert attrs["impact.setup.instrumentations"] == "fake"
    # Nothing of OpenTelemetry, and no library of the application, was imported: a process that never uses an
    # instrumented library (an exec probe, a helper) costs almost nothing.
    assert otel_modules(got) == []
    assert "fakelib" not in got["loaded"]
    assert not has_span(sink, "fake.instrumented")


def test_importing_the_library_applies_the_instrumentation(layout, sink):
    got = parse(layout.run(USE_LIBRARY, layout.env(sink)))
    assert "opentelemetry.sdk.trace" in got["loaded"]
    assert wait_for(lambda: has_span(sink, "fake.instrumented"))


def test_the_instrumented_spans_carry_the_workload_placement(layout, sink):
    layout.run(USE_LIBRARY, layout.env(sink))
    assert wait_for(lambda: has_span(sink, "fake.instrumented"))
    resources = [
        {a["key"]: a["value"].get("stringValue") for a in rs["resource"]["attributes"]}
        for r in sink.requests
        for rs in json.loads(gzip.decompress(r["body"]))["resourceSpans"]
    ]
    assert resources
    assert all(r["k8s.namespace.name"] == "shop" and r["service.name"] == "support-assistant" for r in resources)


def test_the_package_never_takes_the_applications_global_tracer_provider(layout, sink):
    code = "import fakelib\nfrom opentelemetry import trace\nprint(type(trace.get_tracer_provider()).__name__)\n"
    result = layout.run(code, layout.env(sink))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ProxyTracerProvider"  # still the API default: the application can set its own


def test_instrumentors_outside_the_payload_are_not_applied(layout, sink):
    layout.add_library("theirlib")
    layout.add_instrumentor("theirs", "TheirInstrumentor", library="theirlib", where=layout.app_site)
    layout.run("import fakelib, theirlib\n", layout.env(sink))
    assert wait_for(lambda: has_span(sink, "fake.instrumented"))
    assert not has_span(sink, "theirs.instrumented")


def test_disabled_instrumentations_are_not_armed(layout, sink):
    layout.run(USE_LIBRARY, layout.env(sink, OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="fake"))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    assert setup_attributes(sink)["impact.setup.instrumentations"] == ""
    assert not has_span(sink, "fake.instrumented")


def test_a_library_that_is_not_installed_is_not_armed_and_not_mentioned(layout, sink):
    layout.add_instrumentor("absent", "AbsentInstrumentor", library="library-that-is-not-installed", constraint=">=1")
    result = layout.run(USE_LIBRARY, layout.env(sink))
    assert result.returncode == 0
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    attrs = setup_attributes(sink)
    assert attrs["impact.setup.instrumentations"] == "fake"
    assert "impact.setup.skipped" not in attrs
    assert "library-that-is-not-installed" not in result.stderr


def test_an_unsupported_library_version_is_reported_so_the_row_can_say_why(layout, sink):
    # anthropic 2.x against an instrumentation for anthropic < 2: nothing is instrumented and the report says so.
    layout.add_library("somelib", "2.4.0")
    layout.add_instrumentor("versioned", "VersionedInstrumentor", library="somelib", constraint="<2")
    layout.run("import fakelib, somelib\n", layout.env(sink))
    assert wait_for(lambda: any("impact.setup.skipped" in r for r in setup_reports(sink)))
    skipped = next(r for r in setup_reports(sink) if "impact.setup.skipped" in r)
    assert "somelib" in skipped["impact.setup.skipped"]
    assert "2.4.0" in skipped["impact.setup.skipped"]
    assert skipped["impact.setup.instrumentations"] == "fake"  # what was applied by then
    assert wait_for(lambda: has_span(sink, "fake.instrumented"))
    assert not has_span(sink, "versioned.instrumented")


def test_defaults_are_applied_to_the_process_environment(layout, sink):
    code = (
        "import os, json\n"
        "print(json.dumps({k: os.environ.get(k) for k in ['OTEL_SEMCONV_STABILITY_OPT_IN',"
        "'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT','OTEL_PYTHON_EXCLUDED_URLS']}))\n"
    )
    got = parse(layout.run(code, layout.env(sink)))
    assert got["OTEL_SEMCONV_STABILITY_OPT_IN"] == "gen_ai_latest_experimental"
    assert got["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_ONLY"
    assert got["OTEL_PYTHON_EXCLUDED_URLS"] == defaults.DEFAULTS["OTEL_PYTHON_EXCLUDED_URLS"]


# --- AE-11: the AI libraries and agent frameworks, found in the environment, never imported -------------------------


def add_agent_instrumentor(layout: Layout, name: str, library: str, constraint: str = "") -> None:
    layout.add_instrumentor(
        name, f"{name.title().replace('_', '')}Instrumentor", library=library, constraint=constraint
    )


def test_the_setup_report_lists_the_ai_libraries_of_the_environment_and_those_the_image_cannot_trace(layout, sink):
    for dist in ("openai_agents-0.22.1", "langgraph-1.2.11", "openai-3.42.1", "crewai-1.15.35", "mcp-2.2.0"):
        layout.add_installed(dist)
    layout.add_installed("fastapi-0.162.0")  # not an AI library
    add_agent_instrumentor(layout, "openai", "openai")
    add_agent_instrumentor(layout, "openai_agents", "openai-agents", ">=0.3.3")
    add_agent_instrumentor(layout, "langchain", "langchain", ">=0.3.22")
    got = parse(layout.run(STATE, layout.env(sink)))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    attrs = setup_attributes(sink)
    assert attrs["impact.setup.state"] == "active"
    assert attrs["impact.setup.frameworks"] == [
        "crewai 1.15.35",
        "langgraph 1.2.11",
        "mcp 2.2.0",
        "openai-agents 0.22.1",
        "openai 3.42.1",
    ]
    assert attrs["impact.setup.untraced"] == ["crewai 1.15.35", "mcp 2.2.0"]
    assert "crewai" not in got["loaded"]  # a listing, nothing imported


def test_the_setup_report_names_the_frameworks_of_an_environment_with_none(layout, sink):
    layout.run(STATE, layout.env(sink))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    attrs = setup_attributes(sink)
    assert attrs["impact.setup.frameworks"] == []
    assert attrs["impact.setup.untraced"] == []


def test_a_stood_down_workload_still_reports_its_frameworks(layout, sink):
    for dist in ("ddtrace-2.9.1", "openai_agents-0.22.1", "crewai-1.15.35"):
        layout.add_installed(dist)
    add_agent_instrumentor(layout, "openai_agents", "openai-agents", ">=0.3.3")
    layout.run(STATE, layout.env(sink))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    attrs = setup_attributes(sink)
    assert attrs["impact.setup.state"] == "pre-existing"
    assert attrs["impact.setup.frameworks"] == ["crewai 1.15.35", "openai-agents 0.22.1"]
    assert attrs["impact.setup.untraced"] == ["crewai 1.15.35"]


def test_a_failed_start_still_reports_its_frameworks(layout, sink):
    break_the_sdk(layout)
    layout.add_installed("crewai-1.15.35")
    layout.run("import fakelib\n", layout.env(sink))
    assert wait_for(lambda: any(r["impact.setup.state"] == "failed" for r in setup_reports(sink)))
    failed = next(r for r in setup_reports(sink) if r["impact.setup.state"] == "failed")
    assert failed["impact.setup.frameworks"] == ["crewai 1.15.35"]
    assert failed["impact.setup.untraced"] == ["crewai 1.15.35"]


def test_a_library_outside_the_supported_range_moves_to_untraced_when_it_is_found_out(layout, sink):
    layout.add_installed("anthropic-2.4.0")
    layout.add_library("anthropic", "2.4.0")
    add_agent_instrumentor(layout, "anthropic", "anthropic", "<2")
    layout.run("import fakelib, anthropic\n", layout.env(sink))
    assert wait_for(lambda: any("impact.setup.skipped" in r for r in setup_reports(sink)))
    first, skipped = setup_reports(sink)[0], next(r for r in setup_reports(sink) if "impact.setup.skipped" in r)
    assert first["impact.setup.untraced"] == []  # at start nothing is known about the version range
    assert skipped["impact.setup.frameworks"] == ["anthropic 2.4.0"]
    assert skipped["impact.setup.untraced"] == ["anthropic 2.4.0"]


def agents_package(layout: Layout, module: str) -> None:
    (layout.app_site / module).mkdir()
    (layout.app_site / module / "__init__.py").write_text("")


def test_the_openai_agents_instrumentation_is_applied_when_the_application_imports_agents(layout, sink):
    # The distribution is openai-agents; the module the application imports is `agents`.
    layout.add_installed("openai_agents-0.22.1")
    agents_package(layout, "agents")
    add_agent_instrumentor(layout, "openai_agents", "openai-agents", ">=0.3.3")
    got = parse(layout.run("import agents\n" + STATE, layout.env(sink)))
    assert wait_for(lambda: has_span(sink, "openai_agents.instrumented"))
    assert "opentelemetry.sdk.trace" in got["loaded"]


def test_nothing_is_applied_to_an_application_that_never_imports_agents(layout, sink):
    layout.add_installed("openai_agents-0.22.1")
    agents_package(layout, "agents")
    add_agent_instrumentor(layout, "openai_agents", "openai-agents", ">=0.3.3")
    got = parse(layout.run(STATE, layout.env(sink)))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    assert otel_modules(got) == []
    assert not has_span(sink, "openai_agents.instrumented")


def test_a_langgraph_application_without_the_langchain_package_is_instrumented_through_langchain_core(layout, sink):
    # create_react_agent needs langgraph and langchain-core only; the instrumentation is for langchain, whose callbacks
    # live in langchain-core.
    layout.add_installed("langgraph-1.2.11")
    layout.add_installed("langchain_core-1.6.13")
    agents_package(layout, "langchain_core")
    add_agent_instrumentor(layout, "langchain", "langchain", "<2,>=0.3.22")
    layout.run("import langchain_core\n", layout.env(sink))
    assert wait_for(lambda: has_span(sink, "langchain.instrumented"))
    attrs = setup_attributes(sink)
    assert attrs["impact.setup.instrumentations"] == "fake,langchain"


# --- reports: once per container -----------------------------------------------------------------------------------


def test_the_report_is_sent_once_per_container(layout, sink):
    for _ in range(3):
        layout.run(STATE, layout.env(sink))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    time.sleep(0.5)
    assert len(setup_reports(sink)) == 1


def test_a_process_after_the_first_does_not_even_load_the_http_client(layout, sink):
    layout.run(STATE, layout.env(sink))
    second = parse(layout.run(STATE, layout.env(sink)))
    assert second["stdlib_http"] is False


# --- AE-5: stand down ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dist", "detail"),
    [
        ("ddtrace-2.9.1", "ddtrace 2.9.1"),
        ("traceloop_sdk-0.46.0", "traceloop-sdk 0.46.0"),
        ("langfuse-3.2.1", "langfuse 3.2.1"),
        ("openlit-1.34.0", "openlit 1.34.0"),
        ("opentelemetry_instrumentation_openai-0.62.4", "opentelemetry-instrumentation-openai 0.62.4"),
    ],
)
def test_another_tracer_in_the_application_makes_the_package_stand_down(layout, sink, dist, detail):
    layout.add_installed(dist)
    got = parse(layout.run(USE_LIBRARY, layout.env(sink)))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    assert setup_attributes(sink)["impact.setup.state"] == "pre-existing"
    assert setup_attributes(sink)["impact.setup.detail"] == detail
    # Nothing of ours is left in the process: not on sys.path, not in PYTHONPATH, no OpenTelemetry imported.
    assert not [p for p in got["path"] if str(layout.root) in p]
    assert got["pythonpath"] == layout.app_pythonpath
    assert otel_modules(got) == []
    assert not has_span(sink, "fake.instrumented")


def test_an_app_owned_launcher_stands_down_and_hands_the_app_its_own_pythonpath(layout, sink):
    # opentelemetry-instrument builds the child's PYTHONPATH from ours; after stand-down it must see the original.
    launcher = layout.app_path / "opentelemetry-instrument"
    launcher.write_text("import json, os\nprint(json.dumps({'pythonpath': os.environ.get('PYTHONPATH')}))\n")
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(launcher)], env=layout.env(sink), capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pythonpath"] == layout.app_pythonpath
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    assert setup_attributes(sink)["impact.setup.detail"] == "opentelemetry-instrument (launcher)"


def test_a_stood_down_workload_never_sends_to_its_own_endpoint(layout, sink):
    theirs = Sink()
    try:
        layout.add_installed("ddtrace-2.9.1")
        layout.run(STATE, layout.env(sink, OTEL_EXPORTER_OTLP_ENDPOINT=theirs.url))
        assert wait_for(lambda: len(sink.requests) >= 1)
        assert theirs.requests == []
    finally:
        theirs.close()


def test_a_workload_with_its_own_otel_pipeline_in_its_environment_stands_down(layout, sink):
    layout.add_installed("opentelemetry_sdk-1.30.0")
    env = layout.env(sink, OTEL_EXPORTER_OTLP_ENDPOINT="http://collector.observability:4318")
    got = parse(layout.run(STATE, env))
    assert wait_for(lambda: has_span(sink, "impact.setup"))
    assert setup_attributes(sink)["impact.setup.state"] == "pre-existing"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in setup_attributes(sink)["impact.setup.detail"]
    assert otel_modules(got) == []


def test_otel_sdk_disabled_means_no_start_and_no_report(layout, sink):
    got = parse(layout.run(USE_LIBRARY, layout.env(sink, OTEL_SDK_DISABLED="true")))
    time.sleep(0.5)
    assert sink.requests == []
    assert otel_modules(got) == []


# --- AE-7: the application never depends on impact.ai ---------------------------------------------------------------


def test_an_unreachable_gateway_does_not_delay_or_break_the_application(layout):
    started = time.monotonic()
    result = layout.run(USE_LIBRARY, layout.env(None, IMPACT_OTLP_ENDPOINT="http://127.0.0.1:9"))
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 20
    assert "Traceback" not in result.stderr


def break_the_sdk(layout: Layout) -> None:
    """An incompatible copy of one of the SDK's dependencies: importing the SDK raises."""
    (layout.root / "opentelemetry" / "sdk").mkdir()
    (layout.root / "opentelemetry" / "sdk" / "__init__.py").write_text('raise ImportError("incompatible wrapt 1.14")\n')


def test_a_failure_to_start_the_sdk_never_breaks_the_applications_import(layout, sink):
    break_the_sdk(layout)
    code = (
        "import json, os, sys\n"
        "import fakelib  # the import must succeed although the instrumentation cannot start\n"
        "left = sorted(m for m in sys.modules if m.startswith('opentelemetry.sdk'))\n"
        "import opentelemetry.sdk.trace  # and the application's own copy is importable again\n"
        "print(json.dumps({'path': sys.path, 'pythonpath': os.environ.get('PYTHONPATH'), 'left': left,\n"
        "  'sdk_from': opentelemetry.sdk.trace.__file__}))\n"
    )
    got = parse(layout.run(code, layout.env(sink)))
    assert wait_for(lambda: len(setup_reports(sink)) >= 2)
    by_state = {r["impact.setup.state"]: r for r in setup_reports(sink)}
    assert set(by_state) == {"active", "failed"}
    assert "incompatible wrapt 1.14" in by_state["failed"]["impact.setup.detail"]
    assert not [p for p in got["path"] if str(layout.root) in p]
    assert got["pythonpath"] == layout.app_pythonpath
    assert got["left"] == []
    assert str(layout.root) not in got["sdk_from"]


def test_an_instrumentor_that_raises_never_breaks_the_applications_import(layout, sink):
    (layout.root / "fake_instr" / "__init__.py").write_text(
        "from opentelemetry.instrumentation.instrumentor import BaseInstrumentor\n"
        "class FakeInstrumentor(BaseInstrumentor):\n"
        "    def instrumentation_dependencies(self): return ()\n"
        "    def _instrument(self, **kwargs): raise RuntimeError('boom')\n"
        "    def _uninstrument(self, **kwargs): pass\n"
    )
    result = layout.run("import fakelib\nprint('imported')\n", layout.env(sink))
    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


def test_the_next_sitecustomize_still_runs(layout, sink):
    (layout.app_path / "sitecustomize.py").write_text("import os\nos.environ['APP_SITECUSTOMIZE'] = 'ran'\n")
    code = "import os\nprint(os.environ.get('APP_SITECUSTOMIZE'))\n"
    assert layout.run(code, layout.env(sink)).stdout.strip() == "ran"
    layout.add_installed("ddtrace-2.9.1")
    assert layout.run(code, layout.env(sink)).stdout.strip() == "ran"


def test_the_service_name_of_an_injected_workload_comes_from_its_deployment(layout, sink):
    env = layout.env(sink)
    del env["OTEL_SERVICE_NAME"]  # the kit's Instrumentation does not set it
    layout.run(USE_LIBRARY, env)
    assert wait_for(lambda: has_span(sink, "fake.instrumented"))
    names = {
        {a["key"]: a["value"].get("stringValue") for a in rs["resource"]["attributes"]}["service.name"]
        for r in sink.requests
        for rs in json.loads(gzip.decompress(r["body"]))["resourceSpans"]
    }
    assert names == {"support-assistant"}

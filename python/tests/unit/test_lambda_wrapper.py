# SPDX-License-Identifier: Apache-2.0

"""The layer's wrapper and start, end to end in a real interpreter: lambda/impact-ai-instrument runs a stand-in for
Lambda's Python runtime (fake_lambda_runtime.py) the way Lambda runs it, and an OTLP sink stands in for ingest.

The layer is laid out as in the archive (see lambda/build_layer.py), with the package's source and a fake AI library
and instrumentor in place of the payload's. The runtime ends the process right after the last invocation, without exit
handlers, so a span or report that arrived was sent within an invocation. The same checks run in AWS's own Lambda
images under the Runtime Interface Emulator in python/tests/lambda (make test-lambda).
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from helpers import Sink

ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "python"
sys.path.insert(0, str(PYTHON_DIR / "tools"))

import assemble_payload as assemble  # noqa: E402

RUNTIME = Path(__file__).with_name("fake_lambda_runtime.py")
ARN = "arn:aws:lambda:eu-west-1:123456789012:function:refund-bot"
# Lambda runs the wrapper with the runtime's interpreter by its versioned name (/var/lang/bin/python3.12).
INTERPRETER = Path(sys.executable).parent / f"python{sys.version_info.major}.{sys.version_info.minor}"
if not INTERPRETER.exists():
    INTERPRETER = Path(sys.executable)

FAKE_AI_INSTRUMENTOR = """
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor


class FakeAIInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self):
        return ()

    def _instrument(self, **kwargs):
        import fakeai

        tracer = kwargs["tracer_provider"].get_tracer("fakeai")
        original = fakeai.complete

        def complete(prompt):
            with tracer.start_as_current_span("chat fake-model") as span:
                span.set_attribute("gen_ai.request.model", "fake-model")
                return original(prompt)

        fakeai.complete = complete

    def _uninstrument(self, **kwargs):
        pass
"""

APP = """
import fakeai


def handler(event, context):
    if event.get("order") == "A-boom":
        raise ValueError("order not found")
    return {"statusCode": 200, "body": fakeai.complete("refund " + event["order"])}
"""


class Layer:
    """/opt as the layer leaves it, and /var/task with the function's code."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.opt = tmp / "opt"
        self.wrapper = self.opt / "impact-ai-instrument"
        self.root = self.opt / "impact-ai" / "python"
        self.task = tmp / "task"
        self.state = tmp / "state"
        for directory in (self.root, self.opt / "impact-ai" / "lambda", self.task, self.state):
            directory.mkdir(parents=True)
        shutil.copy2(ROOT / "lambda" / "impact-ai-instrument", self.wrapper)
        shutil.copy2(ROOT / "lambda" / "sitecustomize.py", self.opt / "impact-ai" / "lambda" / "sitecustomize.py")
        shutil.copytree(
            PYTHON_DIR / "src" / "impact_instrumentation",
            self.root / "impact_instrumentation",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (self.root / "_impact_shared").mkdir()
        (self.root / "fakeai_instr").mkdir()
        (self.root / "fakeai_instr" / "__init__.py").write_text(FAKE_AI_INSTRUMENTOR)
        info = self.root / "opentelemetry_instrumentation_fakeai-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: opentelemetry-instrumentation-fakeai\nVersion: 1.0\n"
            'Requires-Dist: fakeai; extra == "instruments"\n'
        )
        (info / "entry_points.txt").write_text(
            "[opentelemetry_instrumentor]\nfakeai = fakeai_instr:FakeAIInstrumentor\n"
        )
        package = self.root / "impact_instrumentation"
        (package / "_registry.py").write_text(assemble.generate_registry(self.root))
        table = assemble.read_qualification(PYTHON_DIR / "qualification.json")
        (package / "_qualification.py").write_text(assemble.generate_qualification(table))
        # The function bundles its AI library, as a zip package does.
        (self.task / "fakeai").mkdir()
        (self.task / "fakeai" / "__init__.py").write_text("def complete(prompt):\n    return 'ok: ' + prompt\n")
        (self.task / "fakeai-1.0.dist-info").mkdir()
        (self.task / "fakeai-1.0.dist-info" / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: fakeai\nVersion: 1.0\n"
        )
        (self.task / "app.py").write_text(APP)

    def env(self, endpoint: str | None, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "AWS_LAMBDA_FUNCTION_NAME": "refund-bot",
            "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
            "AWS_REGION": "eu-west-1",
            "LAMBDA_TASK_ROOT": str(self.task),
            "LAMBDA_RUNTIME_DIR": "/var/runtime",
            "_HANDLER": "app.handler",
            "AWS_LAMBDA_EXEC_WRAPPER": str(self.wrapper),
            "IMPACT_STATE_DIR": str(self.state),
            # Only the end of an invocation sends: the batch processor's own schedule would come too late to count.
            "OTEL_BSP_SCHEDULE_DELAY": "60000",
        }
        if endpoint:
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
            env["OTEL_EXPORTER_OTLP_HEADERS"] = "authorization=Bearer%20test-ingest-key"
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def invoke(self, env: dict[str, str], invocations: int = 3, command: list[str] | None = None) -> dict:
        args = [str(self.wrapper), *(command or [str(INTERPRETER), str(RUNTIME), str(invocations)])]
        result = subprocess.run(  # noqa: S603 - test-owned script and interpreter
            args, env=env, capture_output=True, text=True, timeout=60, check=False
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def layer(tmp_path):
    return Layer(tmp_path)


@pytest.fixture
def sink():
    s = Sink()
    yield s
    s.close()


def resources(sink: Sink) -> list[dict]:
    out = []
    for request in sink.requests:
        for resource_spans in json.loads(gzip.decompress(request["body"]))["resourceSpans"]:
            attributes = {a["key"]: next(iter(a["value"].values())) for a in resource_spans["resource"]["attributes"]}
            for scope_spans in resource_spans["scopeSpans"]:
                for span in scope_spans["spans"]:
                    out.append({"span": span["name"], "resource": attributes, "headers": request["headers"]})
    return out


def setup_state(sink: Sink) -> str | None:
    for request in sink.requests:
        for resource_spans in json.loads(gzip.decompress(request["body"]))["resourceSpans"]:
            for scope_spans in resource_spans["scopeSpans"]:
                for span in scope_spans["spans"]:
                    if span["name"] == "impact.setup":
                        return next(
                            a["value"]["stringValue"] for a in span["attributes"] if a["key"] == "impact.setup.state"
                        )
    return None


def results(seen: dict) -> list:
    return [invocation.get("result", invocation.get("error")) for invocation in seen["invocations"]]


EXPECTED = [{"statusCode": 200, "body": f"ok: refund A-{n}"} for n in range(3)]


# --- The function's spans, with the function's resource ---------------------------------------------------------------


def test_the_layer_traces_the_function_and_sends_within_each_invocation(layer, sink):
    seen = layer.invoke(layer.env(sink.url))
    assert results(seen) == EXPECTED
    assert seen["wrapped"] is True
    spans = resources(sink)
    calls = [s for s in spans if s["span"] == "chat fake-model"]
    assert len(calls) == 3, spans
    assert setup_state(sink) == "active"
    for span in spans:
        resource = span["resource"]
        assert resource["faas.name"] == "refund-bot"
        assert resource["service.name"] == "refund-bot"
        assert resource["cloud.provider"] == "aws"
        assert resource["cloud.platform"] == "aws_lambda"
        assert resource["cloud.region"] == "eu-west-1"
        assert resource["cloud.account.id"] == "123456789012"
        assert resource["cloud.resource_id"] == ARN
        assert span["headers"]["authorization"] == "Bearer test-ingest-key"


def test_the_runtime_sees_the_path_it_would_see_without_the_layer(layer, sink):
    seen = layer.invoke(layer.env(sink.url))
    # Lambda's runtime leaves out the layers' /opt/python when PYTHONPATH is set: the start took its entry out again.
    assert seen["pythonpath"] is None
    assert seen["sitecustomize"].endswith(os.path.join("impact-ai", "lambda", "sitecustomize.py"))
    assert not any(p.endswith(os.path.join("impact-ai", "lambda")) for p in seen["path_before"])


def test_a_function_that_sets_pythonpath_keeps_it(layer, sink):
    seen = layer.invoke(layer.env(sink.url, PYTHONPATH=str(layer.task / "vendor")))
    assert seen["pythonpath"] == str(layer.task / "vendor")
    assert results(seen) == EXPECTED
    empty = layer.invoke(layer.env(sink.url, PYTHONPATH=""))
    assert empty["pythonpath"] == ""


def test_the_functions_own_sitecustomize_still_runs(layer, sink):
    own = layer.tmp / "own-site"
    own.mkdir()
    (own / "sitecustomize.py").write_text("import os\nos.environ['OWN_SITECUSTOMIZE'] = 'ran'\n")
    layer.task.joinpath("app.py").write_text(
        APP.replace("import fakeai", "import fakeai, os")
        + ("\n\ndef own(event, context):\n    return os.environ.get('OWN_SITECUSTOMIZE')\n")
    )
    seen = layer.invoke(layer.env(sink.url, PYTHONPATH=str(own), _HANDLER="app.own"))
    assert results(seen) == ["ran", "ran", "ran"]


def test_the_handlers_own_error_reaches_the_runtime_unchanged(layer, sink):
    layer.task.joinpath("app.py").write_text(
        APP + "\n\ndef boom(event, context):\n    return handler({'order': 'A-boom'}, context)\n"
    )
    seen = layer.invoke(layer.env(sink.url, _HANDLER="app.boom"), invocations=2)
    assert results(seen) == [["ValueError", "order not found"]] * 2
    assert seen["wrapped"] is True


def test_a_handler_that_cannot_be_imported_fails_as_it_would_without_the_layer(layer, sink):
    seen = layer.invoke(layer.env(sink.url, _HANDLER="missing_module.handler"))
    assert seen["init_error"] == ["ModuleNotFoundError", "No module named 'missing_module'"]


# --- The function always runs ----------------------------------------------------------------------------------------


class Silent:
    """An ingest that accepts connections and never answers."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.url = f"http://127.0.0.1:{self.server.getsockname()[1]}"
        self.connections = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            self.connections.append(connection)

    def close(self):
        self.server.close()
        for connection in self.connections:
            connection.close()


def test_an_ingest_that_never_answers_costs_each_invocation_at_most_the_flush_bound(layer, sink):
    plain = layer.invoke(layer.env(sink.url, AWS_LAMBDA_EXEC_WRAPPER=None), invocations=3)
    silent = Silent()
    try:
        seen = layer.invoke(layer.env(silent.url, IMPACT_LAMBDA_FLUSH_MS="100"), invocations=3)
    finally:
        silent.close()
    assert results(seen) == EXPECTED
    for invocation in seen["invocations"]:
        assert invocation["seconds"] < 0.1 + 0.15
    assert results(plain) == EXPECTED


def test_after_one_wait_runs_out_an_ingest_that_never_answers_costs_the_invocations_nothing(layer):
    silent = Silent()
    try:
        seen = layer.invoke(layer.env(silent.url), invocations=12)  # the default bound
    finally:
        silent.close()
    first, *rest = seen["invocations"]
    assert all("result" in invocation for invocation in seen["invocations"])
    assert first["seconds"] < 0.2 + 0.15  # the one wait that runs out
    # Well within the proposed +25 ms at the 95th percentile, for every invocation after it.
    assert max(invocation["seconds"] for invocation in rest) < 0.025, rest


@pytest.mark.parametrize("endpoint", [None, "http://127.0.0.1:9"])
def test_without_an_ingest_or_a_key_the_function_runs(layer, endpoint):
    env = layer.env(endpoint)
    env.pop("OTEL_EXPORTER_OTLP_HEADERS", None)
    seen = layer.invoke(env)
    assert results(seen) == EXPECTED


def test_a_broken_package_leaves_the_function_as_it_was(layer, sink):
    (layer.root / "impact_instrumentation" / "aws_lambda.py").write_text("raise RuntimeError('broken layer')\n")
    seen = layer.invoke(layer.env(sink.url))
    assert results(seen) == EXPECTED
    assert seen["pythonpath"] is None
    assert seen["wrapped"] is False
    assert not any(p == str(layer.root) for p in seen["path_before"])


def test_a_failed_start_reports_why_within_the_first_invocation(layer, sink):
    (layer.root / "impact_instrumentation" / "runtime.py").write_text("raise RuntimeError('cannot arm')\n")
    seen = layer.invoke(layer.env(sink.url))
    assert results(seen) == EXPECTED
    assert setup_state(sink) == "failed"
    assert resources(sink)[0]["resource"]["cloud.resource_id"] == ARN


def test_a_layer_that_is_not_all_there_changes_nothing(layer, sink):
    shutil.rmtree(layer.root / "impact_instrumentation")
    seen = layer.invoke(layer.env(sink.url))
    assert results(seen) == EXPECTED
    assert seen["sitecustomize"] is None
    assert seen["pythonpath"] is None


def test_another_runtime_runs_exactly_as_it_would_without_the_layer(layer):
    env = layer.env(None)
    seen = subprocess.run(  # noqa: S603
        [str(layer.wrapper), "/usr/bin/env"], env=env, capture_output=True, text=True, timeout=30, check=True
    ).stdout.splitlines()
    assert not any(line.startswith("PYTHONPATH=") for line in seen)


# --- One wrapper -----------------------------------------------------------------------------------------------------


def test_beside_another_tracers_lambda_wrapper_the_layer_stands_down(layer, sink):
    vendor = layer.task / "datadog_lambda"
    vendor.mkdir()
    (vendor / "__init__.py").write_text("")
    (vendor / "handler.py").write_text(
        "import importlib, os\n"
        "_m, _, _f = os.environ['DD_LAMBDA_HANDLER'].rpartition('.')\n"
        "handler = getattr(importlib.import_module(_m), _f)\n"
    )
    seen = layer.invoke(layer.env(sink.url, _HANDLER="datadog_lambda.handler.handler", DD_LAMBDA_HANDLER="app.handler"))
    assert results(seen) == EXPECTED
    assert seen["wrapped"] is False
    time.sleep(0.2)
    assert sink.requests == []  # a stood-down function reports to the kit's gateway only, which a function has none of


def test_the_wrapper_starts_no_other_wrapper(layer, sink):
    """AWS_LAMBDA_EXEC_WRAPPER holds one script: the layer's never runs a second one it might find in the settings."""
    other = layer.tmp / "other-wrapper"
    other.write_text('#!/bin/sh\necho other-wrapper-ran >&2\nexec "$@"\n')
    other.chmod(0o755)
    env = layer.env(sink.url, OTHER_EXEC_WRAPPER=str(other), ORIGINAL_AWS_LAMBDA_EXEC_WRAPPER=str(other))
    result = subprocess.run(  # noqa: S603
        [str(layer.wrapper), str(INTERPRETER), str(RUNTIME), "1"], env=env, capture_output=True, text=True, timeout=60
    )
    assert "other-wrapper-ran" not in result.stderr
    assert result.returncode == 0

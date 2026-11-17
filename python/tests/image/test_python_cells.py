# SPDX-License-Identifier: Apache-2.0

"""The qualification matrix of the Python image: Python 3.11 to 3.14 by the AI libraries the image traces and their
major versions, against the mock providers. scripts/qualify_python.py names the cells.

Every cell runs a fixture workload with the payload injected, the way the Operator injects it, makes its library's model
calls and asks: did the workload start and serve; did each model call arrive as exactly one span with its provider,
model and tokens; did its content arrive on the span or on a span event; and for an agent framework, did its agent and
tool steps arrive in the trace? python/qualification.json holds the answers, and the package traces a library only where
a passing cell matches the workload. This test holds the file to them: when a cell changes it fails and says to run
`make qualify-python`, which runs every cell again and writes the file.

The cells run with IMPACT_QUALIFICATION_MATRIX=1, which arms what the file does not prove, so that the matrix finds what
works whatever the file says; that the package keeps to the file is test_qualification_gate.py's business.
CELL_LIBRARIES (scripts/python-cells.sh) limits a run to some cells (`library` or `library@python`), QUALIFY_DIR makes
every cell write its answer there instead of comparing it, and CELLS_NEWEST=1 runs each library's newest series on its
fixture with its libraries upgraded to their newest releases (the weekly run): a cell then passes when it proves its
library as the committed cells of that library do, inside the proven range.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import app_image, require_images
from containers import http, run, wait_until

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_python as qualify  # noqa: E402

pytestmark = pytest.mark.cells

PROMPT, ANSWER = "What is the weather in Paris?", "Hello from the stub."
# The operations of a model call, as the instrumentations name them.
MODEL_CALLS = {"chat", "generate_content", "text_completion"}
WRITE_TO = os.environ.get("QUALIFY_DIR")
# The weekly run: each library's newest series, on its fixture with its libraries upgraded to their newest releases
# (scripts/python-cells.sh newest).
NEWEST = os.environ.get("CELLS_NEWEST") == "1"
CELLS = qualify.cells(qualify.parse(os.environ.get("CELL_LIBRARIES") or ""), newest_only=NEWEST)


def fixture_image(python: str, fixture: str) -> str:
    return app_image(python, f"newest-{fixture or 'app'}") if NEWEST else app_image(python, fixture)


VERSION = "import importlib.metadata as m, sys; print(m.version(sys.argv[1]))"


def uvicorn(app: str) -> list[str]:
    return ["uvicorn", f"{app}:app", "--host", "0.0.0.0", "--port", "8000"]  # noqa: S104


def installed(container, image: str, library: str) -> str | None:
    """The version of the library in the workload: read in the running container, or in one of its own."""
    result = run("exec", container.name, "python", "-I", "-c", VERSION, library, check=False)
    if result.returncode != 0:
        result = run("run", "--rm", image, "python", "-I", "-c", VERSION, library, check=False)
    return result.stdout.strip() or None


def is_model_call(span: dict) -> bool:
    return span["name"] != "impact.setup" and span["attrs"].get("gen_ai.operation.name") in MODEL_CALLS


def complete(span: dict) -> bool:
    """The provider, the model and both token counts."""
    attrs = span["attrs"]
    tokens = [attrs.get("gen_ai.usage.input_tokens"), attrs.get("gen_ai.usage.output_tokens")]
    return (
        bool(attrs.get("gen_ai.provider.name"))
        and bool(attrs.get("gen_ai.request.model"))
        and all(isinstance(count, int) and count > 0 for count in tokens)
    )


def model_calls(mock, expected: int) -> list[dict]:
    """The model calls' spans, once `expected` have arrived and a quiet moment has passed: a second span for a call
    arrives in a later batch than the first, so a count taken the moment the expected number is in would miss it."""
    wait_until(lambda: len([s for s in mock.spans() if is_model_call(s)]) >= expected, timeout=30, message="spans")
    time.sleep(1.5)
    return [s for s in mock.spans() if is_model_call(s)]


def on_span(span: dict, text: str) -> bool:
    return text in json.dumps(span["attrs"])


def in_events(span: dict, text: str) -> bool:
    return text in json.dumps([event.get("attributes", []) for event in span.get("events", [])])


def content(calls: list[dict]) -> str | None:
    """Where the prompt (of every call) and the answer (of the last one, at least) arrived: on the span, on a span event
    (some of it, the rest on the span), or nowhere."""
    if all(on_span(s, PROMPT) for s in calls) and any(on_span(s, ANSWER) for s in calls):
        return "span"

    def seen(span: dict, text: str) -> bool:
        return on_span(span, text) or in_events(span, text)

    if all(seen(s, PROMPT) for s in calls) and any(seen(s, ANSWER) for s in calls):
        return "event"
    return None


def steps(mock, calls: list[dict]) -> bool:
    """The agent step, named, and the tool step, named, in the trace of the model calls."""
    traces = {s["traceId"] for s in calls}

    def arrived() -> bool:
        trace = [s["attrs"] for s in mock.spans() if s["traceId"] in traces]
        agents = [a for a in trace if a.get("gen_ai.operation.name") == "invoke_agent" and a.get("gen_ai.agent.name")]
        tools = [a for a in trace if a.get("gen_ai.operation.name") == "execute_tool" and a.get("gen_ai.tool.name")]
        return bool(agents and tools)

    try:
        return bool(wait_until(arrived, timeout=20, message="the agent and tool steps"))
    except AssertionError:
        return False


def measure(start_app, mock, python: str, name: str) -> tuple[dict, dict]:
    """One cell: did the workload start and serve, did its model calls arrive, where is the content, and the steps;
    and what the workload says of its process after the calls: the type of its global tracer provider ("unread" when
    the workload did not serve) and, where it tells, its GenAI log-event switch."""
    variant = qualify.VARIANTS[name]
    image = fixture_image(python, variant.fixture)
    result = {
        "python": python,
        "library": variant.library,
        "major": None,
        "version": None,
        "setting": variant.setting,
        "started": False,
        "traced": False,
        "content": None,
        "steps": False if variant.agent else None,
    }
    process: dict = {"provider": "unread"}
    app = start_app(image, uvicorn(variant.app), env={"IMPACT_QUALIFICATION_MATRIX": "1"})
    try:
        wait_until(lambda: http("GET", f"{app.url}/healthz")[0] == 200, timeout=90, message="the workload to start")
        answers = [http("POST", f"{app.url}{path}", {"prompt": PROMPT}, timeout=90) for path in variant.requests]
        result["started"] = all(answer == (200, {"answer": ANSWER}) for answer in answers)
        if result["started"]:
            calls = model_calls(mock, variant.calls)
            result["traced"] = len(calls) == variant.calls and all(complete(s) for s in calls)
            result["content"] = content(calls)
            if variant.agent:
                result["steps"] = steps(mock, calls)
            # What the process's global tracer provider is once its libraries made their calls.
            process = http("GET", f"{app.url}/otel/global-tracer-provider")[1]
    except AssertionError:
        pass  # the workload did not start, or the spans did not come: the cell says so
    result["version"] = installed(app, image, variant.library)
    result["major"] = qualify.series(result["version"])
    if not (result["started"] and result["traced"]):
        print(f"{python} {name}: {result}\n{app.logs()[-4000:]}")  # what a failing cell shows in the job's log
    return result, process


@pytest.mark.parametrize(("python", "name"), CELLS, ids=[f"python{p}-{n}" for p, n in CELLS])
def test_the_cell_is_what_the_qualification_says(start_app, mock, python, name):
    require_images(fixture_image(python, qualify.VARIANTS[name].fixture))
    result, process = measure(start_app, mock, python, name)
    assert result["version"], f"the workload of {name} on Python {python} has no {qualify.VARIANTS[name].library}"
    if result["started"]:
        # The package gives its provider to each instrumentation and never registers it globally, so after the
        # libraries made their calls the global provider is still OpenTelemetry's default (or OpenTelemetry is not
        # importable by the application at all).
        assert process["provider"] in (None, "ProxyTracerProvider"), f"the global tracer provider is {process}"
        # Nor does an instrumentation leave a switch in the process's environment that the workload did not set (the
        # GenAI log-event switch would send a log event per call to the application's own logger provider).
        assert process.get("genaiEmitEvent") is None, f"the process's environment was changed: {process}"
    if WRITE_TO:
        Path(WRITE_TO).mkdir(parents=True, exist_ok=True)
        (Path(WRITE_TO) / f"python{python}-{name}.json").write_text(json.dumps(result, indent=2) + "\n")
    if NEWEST:
        verdict = qualify.newest_verdict(json.loads(qualify.TABLE.read_text()), result)
        assert verdict.passed, verdict.text
        print(verdict.text)
        return
    if WRITE_TO:
        return
    committed = {qualify.key(c): c for c in json.loads(qualify.TABLE.read_text())["cells"]}
    key = qualify.key(result)
    assert key in committed, f"no cell for {key} in python/qualification.json: run `make qualify-python`"
    assert result == committed[key], (
        f"the cell changed: run `make qualify-python`\n  now:       {result}\n  committed: {committed[key]}"
    )

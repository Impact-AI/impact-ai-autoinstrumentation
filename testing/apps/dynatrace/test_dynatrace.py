# SPDX-License-Identifier: Apache-2.0

"""Apps that export OTLP to a Dynatrace environment: the endpoint mock, the recorded copies and the cells behind them.

  make test-dynatrace     the mock against what Dynatrace's documentation says, and the committed recordings
  make dynatrace-cells    each cell run again from its locked requirements, compared with its recording

Everything here is built from Dynatrace's public documentation and open-source instrumentation; no Dynatrace software
is downloaded or run.
"""

from __future__ import annotations

import base64
import gzip
import http.client
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RECORDINGS = ROOT / "testing" / "fixtures" / "sources" / "dynatrace"
CANARY = "CONTENT-CANARY"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"dynatrace_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass looks its module up there
    spec.loader.exec_module(module)
    return module


harness = load("harness", HERE / "harness.py")
tenant = harness.tenant
otlp_json = load("otlp_json", HERE / "otlp_json.py")

# A minimal ExportTraceServiceRequest in binary protobuf: one resourceSpans with one scopeSpans and one span.
SPAN = b"\x0a\x10" + bytes(16) + b"\x12\x08" + bytes(8) + b"\x2a\x01x"
BODY = b"\x0a" + bytes([len(SPAN) + 4]) + b"\x12" + bytes([len(SPAN) + 2]) + b"\x12" + bytes([len(SPAN)]) + SPAN


@pytest.fixture
def endpoint():
    server = tenant.server("127.0.0.1", 0, [harness.CLASSIC_TOKEN, harness.PLATFORM_TOKEN])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def send(path: str, body: bytes = BODY, headers: dict | None = None, method: str = "POST"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(method, path, body=body or None, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    yield send
    server.shutdown()


def documented(scheme: str = "Api-Token", token: str = harness.CLASSIC_TOKEN, **extra) -> dict:
    return {"Authorization": f"{scheme} {token}", "Content-Type": "application/x-protobuf", **extra}


@pytest.mark.parametrize("path", ["/api/v2/otlp/v1/traces", f"/e/{harness.ENVIRONMENT_ID}/api/v2/otlp/v1/traces"])
@pytest.mark.parametrize("scheme,token", [("Api-Token", harness.CLASSIC_TOKEN), ("Bearer", harness.PLATFORM_TOKEN)])
def test_the_documented_addresses_take_protobuf_with_either_token(endpoint, path, scheme, token):
    status, content_type, body = endpoint(path, headers=documented(scheme, token))
    assert (status, content_type, body) == (200, "application/x-protobuf", b"")


def test_gzip_is_read(endpoint):
    status, _, _ = endpoint("/api/v2/otlp/v1/traces", gzip.compress(BODY), documented(**{"Content-Encoding": "gzip"}))
    assert status == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/x-protobuf"},
        documented(token="dt0c01.UNKNOWN"),  # noqa: S106 - planted
        {"Authorization": harness.CLASSIC_TOKEN, "Content-Type": "application/x-protobuf"},
        {"Authorization": f"Token {harness.CLASSIC_TOKEN}", "Content-Type": "application/x-protobuf"},
    ],
    ids=["no token", "unknown token", "no scheme", "another scheme"],
)
def test_a_request_without_a_token_it_knows_is_refused(endpoint, headers):
    assert endpoint("/api/v2/otlp/v1/traces", headers=headers)[0] == 401


def test_only_binary_protobuf_is_taken(endpoint):
    as_json = documented(**{"Content-Type": "application/json"})
    assert endpoint("/api/v2/otlp/v1/traces", b'{"resourceSpans":[]}', as_json)[0] == 415
    assert endpoint("/api/v2/otlp/v1/traces", b'{"resourceSpans":[]}', documented())[0] == 400
    assert endpoint("/api/v2/otlp/v1/traces", BODY, documented(**{"Content-Encoding": "br"}))[0] == 415


def test_other_signals_are_taken_and_other_paths_are_not(endpoint):
    assert endpoint("/api/v2/otlp/v1/metrics", headers=documented())[0] == 200
    assert endpoint("/api/v2/otlp/v1/logs", headers=documented())[0] == 200
    assert endpoint("/v1/traces", headers=documented())[0] == 404
    assert endpoint("/api/v2/otlp", headers=documented())[0] == 404


def test_a_rule_answers_for_the_endpoint_and_then_lets_it_be(endpoint):
    rules = {"rules": [{"path": "/api/v2/otlp/v1/traces", "status": 503, "times": 1}]}
    endpoint("/_control", json.dumps(rules).encode(), {"Content-Type": "application/json"})
    assert endpoint("/api/v2/otlp/v1/traces", headers=documented())[0] == 503
    assert endpoint("/api/v2/otlp/v1/traces", headers=documented())[0] == 200


def test_what_it_captured_becomes_a_recording(endpoint):
    endpoint("/api/v2/otlp/v1/traces", headers=documented("Bearer", harness.PLATFORM_TOKEN))
    captured = json.loads(endpoint("/_requests", b"", method="GET")[2])
    written = harness.recording.recording(captured, "a test", {}, "2026-11-09", list(harness.TRACES_PATHS))
    (request,) = written["requests"]
    assert (request["method"], request["path"], request["contentType"]) == (
        "POST",
        "/api/v2/otlp/v1/traces",
        "application/x-protobuf",
    )
    assert harness.recording.header(request["headers"], "Authorization") == f"Bearer {harness.PLATFORM_TOKEN}"
    assert base64.b64decode(request["bodyBase64"]) == BODY
    assert otlp_json.request_of(BODY)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "x"


def recording_of(name: str) -> dict:
    return json.loads((RECORDINGS / f"{name}.recording.json").read_text())


def shape(request: dict) -> list:
    """What a tracer version decides: each span's scope, name, kind and attribute keys, without ids, times or values."""
    spans = []
    for resource in request["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                keys = sorted(attribute["key"] for attribute in span.get("attributes", []))
                spans.append((scope["scope"].get("name", ""), span["name"], span.get("kind", 0), keys))
    return sorted(spans)


@pytest.mark.parametrize("name", sorted(harness.CELLS))
def test_each_cell_has_its_recording_as_the_documented_endpoint_received_it(name):
    cell = harness.CELLS[name]
    recorded = recording_of(name)
    assert recorded["packages"] == harness.packages_of(cell)
    assert recorded["requests"], "the endpoint received nothing"
    for request in recorded["requests"]:
        assert request["method"] == "POST"
        assert request["path"] in harness.TRACES_PATHS
        assert request["contentType"] == "application/x-protobuf"
        assert harness.recording.header(request["headers"], "Authorization") == f"{cell.scheme} {cell.token}"
    expected_path = harness.TRACES_PATHS[0] if cell.address == "saas" else harness.TRACES_PATHS[1]
    assert {request["path"] for request in recorded["requests"]} == {expected_path}


@pytest.mark.parametrize("name", sorted(harness.CELLS))
def test_each_recording_is_the_whole_interaction_in_one_trace_with_its_content(name):
    request = otlp_json.merged(recording_of(name)["requests"])
    spans = [
        span for resource in request["resourceSpans"] for scope in resource["scopeSpans"] for span in scope["spans"]
    ]
    assert len({span["traceId"] for span in spans}) == 1
    assert len(spans) >= 5  # the agent, two model calls, the tool and the retrieval
    assert CANARY in json.dumps(request)


def interpreter(cell) -> str:
    """The cell's own environment, made from its locked requirements when it is not there yet."""
    if cell.language == "nodejs":
        folder = HERE / cell.requirements
        if not (folder / "node_modules").exists():
            subprocess.run(  # noqa: S603 - npm with the cell's own lock
                ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],  # noqa: S607
                cwd=folder,
                check=True,
            )
        return "node"
    target = ROOT / "build" / "dynatrace" / cell.name
    python = target / "bin" / "python"
    if not python.exists():
        subprocess.run(["uv", "venv", "-q", "-p", "3.12", str(target)], check=True)  # noqa: S603,S607 - uv from mise
        subprocess.run(  # noqa: S603 - uv from mise, with the cell's locked requirements
            ["uv", "pip", "install", "-q", "--require-hashes", "-r", str(HERE / cell.requirements)],  # noqa: S607
            env={**os.environ, "VIRTUAL_ENV": str(target)},
            check=True,
        )
    return str(python)


@pytest.mark.skipif(not os.environ.get("DYNATRACE_CELLS"), reason="make dynatrace-cells runs the apps")
@pytest.mark.parametrize("name", sorted(harness.CELLS))
def test_cell_sends_what_it_recorded(name):
    cell = harness.CELLS[name]
    result, captured = harness.run(cell, interpreter(cell))
    assert result.returncode == 0, result.stdout + result.stderr
    traces = [record for record in captured if record["path"] in harness.TRACES_PATHS]
    assert traces and all(record["status"] == 200 for record in captured)
    tokens = {harness.recording.header(record["headers"], "Authorization") for record in traces}
    assert tokens == {f"{cell.scheme} {cell.token}"}
    assert shape(otlp_json.merged(traces)) == shape(otlp_json.merged(recording_of(name)["requests"]))

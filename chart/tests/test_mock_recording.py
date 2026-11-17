# SPDX-License-Identifier: Apache-2.0

"""testing/mock/recording.py turns what the recording mock captured into a recording for the relay's decoder tests.

The recording's format is read by reporter/internal/relay/relaytest, which refuses a field it does not know: the keys
written here are the keys of the recording committed there.
"""

from __future__ import annotations

import base64
import gzip
import http.client
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MOCK = ROOT / "testing" / "mock"
GO_RECORDING = ROOT / "reporter" / "internal" / "relay" / "relaytest" / "testdata" / "example.recording.json"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"mock_{name}", MOCK / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def captured():
    """Two requests sent to the real mock, and what its GET /_requests answered."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), load("server").Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def send(method: str, path: str, body: bytes = b"", headers: dict | None = None) -> bytes:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(method, path, body=body or None, headers=headers or {})
            return connection.getresponse().read()
        finally:
            connection.close()

    try:
        send(
            "PUT",
            "/v0.4/traces?lang=python",
            gzip.compress(b"spans", mtime=0),
            {"Content-Type": "application/msgpack", "Content-Encoding": "gzip", "Datadog-Meta-Lang": "python"},
        )
        send("POST", "/telemetry/proxy/api/v2/apmtelemetry", b"{}", {"Content-Type": "application/json"})
        yield json.loads(send("GET", "/_requests"))
    finally:
        httpd.shutdown()


def test_a_captured_request_becomes_a_recorded_request(captured):
    module = load("recording")
    result = module.recording(captured, "a test", {"ddtrace": "3.0.0"}, "2026-11-09", ["/v0.4/traces"])
    assert [r["path"] for r in result["requests"]] == ["/v0.4/traces"]
    request = result["requests"][0]
    assert (request["method"], request["query"]) == ("PUT", "lang=python")
    assert (request["contentType"], request["contentEncoding"]) == ("application/msgpack", "gzip")
    assert request["headers"]["Datadog-Meta-Lang"] == "python"
    assert gzip.decompress(base64.b64decode(request["bodyBase64"])) == b"spans"
    assert (result["how"], result["packages"], result["recorded"]) == ("a test", {"ddtrace": "3.0.0"}, "2026-11-09")
    assert len(module.recording(captured, "a test", {}, "2026-11-09")["requests"]) == 2


def test_the_recording_has_the_keys_the_go_helper_reads(captured):
    written = load("recording").recording(captured, "a test", {}, "2026-11-09")
    committed = json.loads(GO_RECORDING.read_text())
    assert set(written) == set(committed)
    for request in committed["requests"]:
        assert set(written["requests"][0]) == set(request)


def test_the_command_reads_a_file_and_names_its_packages(captured, tmp_path, capsys):
    module = load("recording")
    path = tmp_path / "captured.json"
    path.write_text(json.dumps(captured))
    argv = [str(path), "--how", "a test", "--package", "ddtrace=3.0.0", "--recorded", "2026-11-09"]
    assert module.main([*argv, "--path", "/v0.4/traces"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["packages"] == {"ddtrace": "3.0.0"}
    assert [r["path"] for r in result["requests"]] == ["/v0.4/traces"]
    with pytest.raises(SystemExit):
        module.main([str(path), "--how", "a test", "--package", "ddtrace"])

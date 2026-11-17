# SPDX-License-Identifier: Apache-2.0

"""Regressions from the first public release's CodeQL findings."""

import importlib.util
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gemini_rejects_a_long_non_model_path_without_backtracking():
    code = """
import runpy
mock = runpy.run_path('testing/mock/server.py')
assert mock['gemini_answer']('/' + '/9' * 100000, '') is None
assert mock['gemini_answer']('/models/' + '.' * 100000 + ':other', '') is None
"""
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, timeout=3)  # noqa: S603


@pytest.mark.parametrize("prefix", ["", "/v1beta", "/v1/projects/p/locations/l/publishers/google"])
@pytest.mark.parametrize("method", ["generateContent", "streamGenerateContent"])
def test_gemini_keeps_both_provider_paths_and_streaming(prefix, method):
    mock = load("mock_release_security", "testing/mock/server.py")
    kind, body = mock.gemini_answer(f"{prefix}/models/test-model:{method}", "alt=sse")
    assert kind == ("text/event-stream" if method.startswith("stream") else "application/json")
    assert b"test-model" in body
    assert mock.gemini_answer(f"{prefix}/models/test-model:other", "") is None


@pytest.mark.parametrize("minimum", [ssl.TLSVersion.MINIMUM_SUPPORTED, ssl.TLSVersion.TLSv1_3])
def test_otlp_does_not_accept_a_tls_default_older_than_12(monkeypatch, minimum):
    sys.path.insert(0, str(ROOT / "python" / "src"))
    from impact_instrumentation import otlp

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = minimum
    otlp._TLS_CONTEXTS.clear()
    monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: context)
    try:
        assert otlp.tls_context().minimum_version == max(minimum, ssl.TLSVersion.TLSv1_2)
    finally:
        otlp._TLS_CONTEXTS.clear()


@pytest.mark.parametrize("minimum", [ssl.TLSVersion.MINIMUM_SUPPORTED, ssl.TLSVersion.TLSv1_3])
def test_datadog_stub_does_not_accept_a_tls_default_older_than_12(monkeypatch, minimum):
    backend = load("datadog_release_security", "testing/apps/datadog/backend.py")

    class Context:
        minimum_version = minimum

        def load_cert_chain(self, *args):
            assert backend.tls is None
            assert self.minimum_version == max(minimum, ssl.TLSVersion.TLSv1_2)

    class Server:
        def __init__(self, *args):
            assert backend.tls is context

        def serve_forever(self):
            pass

    context = Context()
    monkeypatch.setattr(ssl, "SSLContext", lambda *args: context)
    monkeypatch.setattr(backend, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(sys, "argv", ["backend", "--cert", "fixture.pem", "--key", "fixture.key"])
    backend.main()
    assert context.minimum_version == max(minimum, ssl.TLSVersion.TLSv1_2)


@pytest.mark.parametrize(
    "tail",
    ["", ":generateContent", "a/b:generateContent", "a:b:generateContent", "a:generateContent/", "a:generateContent\n"],
)
def test_gemini_rejects_malformed_model_methods(tail):
    mock = load("mock_release_security", "testing/mock/server.py")
    assert mock.gemini_answer("/models/" + tail, "") is None

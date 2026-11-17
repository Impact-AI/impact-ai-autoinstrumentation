# SPDX-License-Identifier: Apache-2.0

"""requirements.lock: every package pinned by hash, no provider SDK, no shadowing HTTP stack (C7)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import assemble_payload as assemble  # noqa: E402

LOCK = assemble.parse_lock((ROOT / "requirements.lock").read_text())


def test_every_package_is_pinned_by_hash():
    assert LOCK
    assert all(version and hashes for version, hashes in LOCK.values())
    assert all(h.startswith("sha256:") and len(h) == len("sha256:") + 64 for _, hashes in LOCK.values() for h in hashes)


def test_c7_pins():
    pins = {name: version for name, (version, _) in LOCK.items()}
    # One release of the GenAI packages, tested together by their project: they share one util-genai.
    for package in (
        "opentelemetry-util-genai",
        "opentelemetry-instrumentation-genai-openai",
        "opentelemetry-instrumentation-genai-anthropic",
        "opentelemetry-instrumentation-genai-openai-agents",  # agents: the OpenAI Agents SDK
        "opentelemetry-instrumentation-genai-langchain",  # agents: LangChain and LangGraph
        "opentelemetry-instrumentation-google-genai",  # Google's SDK: the Gemini API and Vertex AI
    ):
        assert pins[package] == "1.3b0", package
    assert pins["opentelemetry-instrumentation-bedrock"] == "0.62.4"  # OpenLLMetry, not botocore's


def test_the_agent_frameworks_themselves_are_not_in_the_payload():
    # The application has its own openai-agents, langchain and langgraph; only the instrumentation is ours.
    for name in ("openai-agents", "langchain", "langchain-core", "langgraph", "langchain-openai", "openai"):
        assert name not in LOCK, name


def test_google_s_sdk_and_its_dependencies_are_not_in_the_payload():
    # The application has its own google-genai; the instrumentation adds no package of Google's to the payload.
    for name in ("google-genai", "google-auth", "google-cloud-aiplatform", "protobuf", "httpx", "websockets"):
        assert name not in LOCK, name


@pytest.mark.parametrize("name", sorted(assemble.FORBIDDEN_DISTRIBUTIONS))
def test_forbidden_distributions_are_not_locked(name):
    assert name not in LOCK


def test_botocore_instrumentation_is_not_locked():
    # It would put Bedrock content in logs, which ingest does not take (qualification of 2026-11-03).
    assert "opentelemetry-instrumentation-botocore" not in LOCK


def test_the_shared_libraries_are_all_locked():
    assert assemble.SHARED <= set(LOCK)


def test_every_instrumentation_pin_is_in_requirements_in():
    wanted = {
        line.split("==")[0].strip()
        for line in (ROOT / "requirements.in").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert wanted <= set(LOCK)


def test_parse_lock_rejects_a_package_without_hashes():
    with pytest.raises(ValueError, match="without hashes"):
        assemble.parse_lock("foo==1.0\nbar==2.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")


def test_parse_lock_rejects_a_hash_without_a_package():
    with pytest.raises(ValueError, match="hash without a package"):
        assemble.parse_lock("    --hash=sha256:" + "a" * 64 + "\n")


def test_requirements_round_trip(tmp_path):
    out = tmp_path / "r.txt"
    assemble.write_requirements({"foo": ("1.0", ["sha256:" + "a" * 64, "sha256:" + "b" * 64])}, out)
    assert assemble.parse_lock(out.read_text()) == {"foo": ("1.0", ["sha256:" + "a" * 64, "sha256:" + "b" * 64])}


def test_the_payload_install_copies_files_out_of_the_uv_cache(monkeypatch, tmp_path):
    # The payload is changed in place after the install; a hardlinked file would change uv's cache with it.
    seen = []
    monkeypatch.setattr(assemble.subprocess, "run", lambda command, check: seen.append(command))
    assemble.uv_install("uv", tmp_path / "requirements.txt", tmp_path / "root", "x86_64-manylinux_2_28", "3.12")
    (command,) = seen
    assert command[command.index("--link-mode") + 1] == "copy"

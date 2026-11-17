# SPDX-License-Identifier: Apache-2.0

"""The coexistence list: the package starts beside a tool only where a passing proof says so, else it stands down.

The mechanism is proven with a generic fixture list, never with the shipped one: the shipped list holds a tool only with
a passing proof, named by the test that showed it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from impact_instrumentation import coexistence, detect

ROOT = Path(__file__).resolve().parents[3]
SHIPPED = Path(coexistence.FILE)
PYTHON = "3.12"
CELL_KEYS = {"tool", "mode", "python", "major", "version", "started", "traced", "same", "proof"}


def cell(tool: str = "openlit", major: str = "1", *, python: str = PYTHON, **answers) -> dict:
    """A cell of the generic fixture: the tool's series `major` proven on `python`, unless an answer says otherwise."""
    return {
        "tool": tool,
        "mode": "alongside",
        "python": python,
        "major": major,
        "version": f"{major}.4.0",
        "started": True,
        "traced": True,
        "same": True,
        "proof": "python/tests/image/test_example.py",
        **answers,
    }


def fixture(*cells: dict) -> dict:
    return {"schema": coexistence.SCHEMA, "cells": list(cells)}


def found(dist: str, version: str) -> tuple[list[detect.Finding], detect.Tool]:
    findings = [detect.Finding("distribution", dist, version)]
    return findings, detect.tool(findings, {dist: version}, {})


# --- the shipped list -------------------------------------------------------------------------------------------------


def test_the_shipped_list_is_a_coexistence_list():
    table = coexistence.load()
    assert table["schema"] == "impact.coexistence.v1"
    assert json.loads(SHIPPED.read_text()) == table


def test_every_shipped_cell_has_its_answers_and_names_a_proof_that_exists():
    for entry in coexistence.load()["cells"]:
        assert set(entry) == CELL_KEYS, entry
        assert entry["mode"] in coexistence.MODES
        assert (ROOT / entry["proof"]).is_file(), entry["proof"]


def test_an_alongside_entry_is_shipped_only_with_a_passing_proof():
    # A failed cell may stay in the list as a record (it breaks nothing), but a tool is alongside only where one passes.
    tools = {entry["tool"] for entry in coexistence.load()["cells"] if entry["mode"] == "alongside"}
    for tool in tools:
        assert any(coexistence.passes(c) for c in coexistence.load()["cells"] if c["tool"] == tool), tool


# --- the mechanism, on the generic fixture -------------------------------------------------------------------------


def test_a_tool_a_passing_cell_proves_is_run_alongside():
    findings, tool = found("openlit", "1.34.0")
    assert coexistence.alongside(findings, tool, table=fixture(cell(major="1")), python=PYTHON)


@pytest.mark.parametrize(
    ("why", "table", "version", "python"),
    [
        ("an empty list", fixture(), "1.34.0", PYTHON),
        ("another tool", fixture(cell(tool="langfuse")), "1.34.0", PYTHON),
        ("another series", fixture(cell(major="1")), "2.0.1", PYTHON),
        ("a series between two proven ones", fixture(cell(major="1"), cell(major="3")), "2.0.1", PYTHON),
        ("another Python", fixture(cell(python="3.11")), "1.34.0", PYTHON),
        ("it did not start", fixture(cell(started=False)), "1.34.0", PYTHON),
        ("its spans did not arrive", fixture(cell(traced=False)), "1.34.0", PYTHON),
        ("the tool's backend recorded something else", fixture(cell(same=False)), "1.34.0", PYTHON),
        ("no answer", fixture(cell(same=None)), "1.34.0", PYTHON),
        ("a copy cell, not an alongside one", fixture(cell(mode="copy")), "1.34.0", PYTHON),
        ("no version", fixture(cell(major="1")), None, PYTHON),
    ],
)
def test_anything_else_keeps_the_stand_down(why, table, version, python):
    findings = [detect.Finding("distribution", "openlit", version)]
    tool = detect.tool(findings, {"openlit": version}, {})
    assert not coexistence.alongside(findings, tool, table=table, python=python), why


def test_a_0x_tool_is_proven_by_its_minor():
    findings, tool = found("openlit", "0.22.3")
    assert coexistence.alongside(findings, tool, table=fixture(cell(major="0.22")), python=PYTHON)
    findings, tool = found("openlit", "0.23.0")
    assert not coexistence.alongside(findings, tool, table=fixture(cell(major="0.22")), python=PYTHON)


def test_every_finding_must_show_the_proven_tool():
    # The workload also exports through its own OpenTelemetry pipeline: another tool, so the package stands down.
    findings = [
        detect.Finding("distribution", "openlit", "1.34.0"),
        detect.Finding("environment", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    ]
    tool = detect.tool(findings, {"openlit": "1.34.0"}, {})
    assert tool.id == "openlit"
    assert not coexistence.alongside(findings, tool, table=fixture(cell()), python=PYTHON)
    # So does a finding of no known tool.
    findings = [detect.Finding("distribution", "openlit", "1.34.0"), detect.Finding("distribution", "agentops", "0.4")]
    assert not coexistence.alongside(findings, tool, table=fixture(cell()), python=PYTHON)


def test_no_finding_or_no_tool_is_never_alongside():
    assert not coexistence.alongside([], detect.Tool("openlit", "1.34.0"), table=fixture(cell()), python=PYTHON)
    findings = [detect.Finding("distribution", "agentops", "0.4.1")]
    assert detect.tool(findings, {"agentops": "0.4.1"}, {}) is None
    assert not coexistence.alongside(findings, None, table=fixture(cell(tool="agentops")), python=PYTHON)


@pytest.mark.parametrize(
    "text", ["{", "[]", '{"schema": "impact.python.qualification.v1", "cells": []}', '{"cells": {}}']
)
def test_a_list_that_cannot_be_read_proves_nothing(tmp_path, monkeypatch, text):
    broken = tmp_path / "coexistence.json"
    broken.write_text(text)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        coexistence.load(str(broken))
    monkeypatch.setattr(coexistence, "FILE", str(broken))
    findings, tool = found("openlit", "1.34.0")
    assert not coexistence.alongside(findings, tool, python=PYTHON)


def test_a_missing_list_proves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(coexistence, "FILE", str(tmp_path / "missing.json"))
    findings, tool = found("openlit", "1.34.0")
    assert not coexistence.alongside(findings, tool, python=PYTHON)


# --- what the report says ------------------------------------------------------------------------------------------


def test_the_report_names_the_tool_its_version_its_exporter_and_whether_the_package_started_beside_it():
    assert coexistence.attributes(detect.Tool("langsmith", "0.14.17", "otlp-http"), False) == {
        "impact.setup.tool": "langsmith",
        "impact.setup.tool.version": "0.14.17",
        "impact.setup.exporter": "otlp-http",
        "impact.setup.alongside": False,
    }
    assert coexistence.attributes(detect.Tool("langsmith", "0.14.17"), True) == {
        "impact.setup.tool": "langsmith",
        "impact.setup.tool.version": "0.14.17",
        "impact.setup.alongside": True,
    }
    assert coexistence.attributes(detect.Tool("datadog"), False) == {
        "impact.setup.tool": "datadog",
        "impact.setup.alongside": False,
    }
    assert coexistence.attributes(None, True) == {}

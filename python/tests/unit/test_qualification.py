# SPDX-License-Identifier: Apache-2.0

"""An AI library is traced only where the image's qualification proves the workload: its Python, the library and its
version, inside the range from the oldest passing cell of that Python to the newest (a cell is a major version, or the
minor of a library that versions by minor). Anything else starts as it would without the kit for that library, and the
setup report says what Impact AI traces and what the workload runs."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from impact_instrumentation import qualification

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_python  # noqa: E402


def cell(python: str, library: str, major: str, **answers) -> dict:
    return {
        "python": python,
        "library": library,
        "major": major,
        "version": f"{major}.9.0" if not major.startswith("0.") else f"{major}.3",
        "setting": "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY",
        "started": answers.get("started", True),
        "traced": answers.get("traced", True),
        "content": answers.get("content", "span"),
        "steps": answers.get("steps"),
    }


def table(*cells: dict, excluded=()) -> dict:
    return {"schema": qualification.SCHEMA, "cells": list(cells), "excluded": list(excluded)}


OPENAI = table(*[cell(python, "openai", major) for python in ("3.12", "3.13") for major in ("4", "5", "6", "7")])


def why(of: dict, library: str, version: str | None, python: str = "3.12") -> str | None:
    return qualification.why_not(of, python=python, library=library, version=version)


# --- what a cell proves --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "proves"),
    [
        ("3.42.1", "3"),
        ("1.75.0", "1"),
        ("2.34.0", "2"),
        ("0.22.3", "0.22"),  # a library that versions by minor is ranged by minor
        ("0.23.0b1", "0.23"),
        ("3.50.0rc1", "3"),  # a pre-release is inside its major version
        ("2026.1.0", "2026"),
        ("0", "0"),
        ("v1.0", None),
        ("", None),
        (None, None),
    ],
)
def test_a_cell_proves_a_major_version_or_the_minor_of_a_zero_version(version, proves):
    assert qualification.series(version) == proves
    assert qualify_python.series(version) == proves  # the matrix files cells by the same rule


def test_a_cell_passes_when_the_workload_served_and_the_calls_arrived_and_an_agent_framework_s_steps_did_too():
    assert qualification.passes(cell("3.12", "openai", "3"))
    assert qualification.passes(cell("3.12", "langgraph", "1", steps=True))
    assert not qualification.passes(cell("3.12", "langgraph", "1", steps=False))
    assert not qualification.passes(cell("3.12", "openai", "3", traced=False))
    assert not qualification.passes(cell("3.12", "openai", "3", started=False))
    assert qualification.passes(cell("3.12", "openai", "3", content=None)), "content is recorded, not required"


# --- the words of the report ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passing", "failing", "said"),
    [
        (["4", "5", "6", "7"], [], "4 to 7"),
        (["7", "4", "6", "5"], [], "4 to 7"),
        (["3"], [], "3"),
        (["1", "3"], [], "1 to 3"),  # the range runs from the oldest passing cell to the newest
        (["1", "3"], ["2"], "1 and 3"),  # a failing cell breaks it
        (["1", "2", "3", "5"], ["4"], "1 to 3 and 5"),
        (["0.3", "0.24"], [], "0.3 to 0.24"),
        (["0.3", "1"], [], "0.3 to 1"),
        (["0.2", "0.4", "1"], ["0.3"], "0.2 and 0.4 to 1"),
        (["3"], ["1"], "3"),  # a failing cell outside the passing ones changes nothing
    ],
)
def test_a_proven_range_is_said_as_a_sentence_says_it_without_a_comma(passing, failing, said):
    assert qualification.spoken(passing, failing) == said


@pytest.mark.parametrize(
    ("pythons", "said"),
    [
        (["3.11", "3.12", "3.13"], "3.11 to 3.13"),
        (["3.12"], "3.12"),
        (["3.11", "3.12"], "3.11 to 3.12"),
        (["3.11", "3.13", "3.14"], "3.11 and 3.13 to 3.14"),
    ],
)
def test_pythons_are_said_without_a_comma(pythons, said):
    assert qualification.spoken_pythons(pythons) == said


def test_a_version_above_the_proven_range_reads_what_impact_ai_traces_then_what_the_workload_runs():
    # The sentence the Node.js package says of an older Node, for a library.
    assert why(OPENAI, "openai", "8.0.0") == "Impact AI traces openai 4 to 7; this workload runs openai 8.0.0"
    assert why(OPENAI, "openai", "3.42.1") == "Impact AI traces openai 4 to 7; this workload runs openai 3.42.1"


def test_a_version_inside_the_proven_range_is_traced_without_waiting_for_a_cell_of_its_own():
    assert why(OPENAI, "openai", "6.0.0") == ""
    assert why(OPENAI, "openai", "7.99.1") == ""
    assert why(OPENAI, "openai", "5.0.0rc1") == ""


def test_a_version_between_the_oldest_and_the_newest_passing_cell_is_traced():
    # The six libraries traced before the qualification have a cell at the floor of their range and one at its top.
    ends = table(cell("3.12", "openai", "1"), cell("3.12", "openai", "3"))
    assert why(ends, "openai", "2.54.0") == ""
    assert why(ends, "openai", "1.26.0") == ""
    assert why(ends, "openai", "0.28.1") == "Impact AI traces openai 1 to 3; this workload runs openai 0.28.1"
    assert why(ends, "openai", "4.0.0") == "Impact AI traces openai 1 to 3; this workload runs openai 4.0.0"
    minors = table(cell("3.12", "anthropic", "0.51"), cell("3.12", "anthropic", "2"))
    for version in ("0.51.0", "0.80.2", "0.125.0", "1.17.0", "2.7.0"):
        assert why(minors, "anthropic", version) == "", version
    assert (
        why(minors, "anthropic", "0.50.0")
        == "Impact AI traces anthropic 0.51 to 2; this workload runs anthropic 0.50.0"
    )


def test_a_failing_cell_between_the_ends_breaks_the_range():
    broken = table(cell("3.12", "openai", "1"), cell("3.12", "openai", "2", traced=False), cell("3.12", "openai", "3"))
    assert why(broken, "openai", "2.54.0") == "Impact AI traces openai 1 and 3; this workload runs openai 2.54.0"
    assert why(broken, "openai", "1.109.1") == ""
    assert why(broken, "openai", "3.42.1") == ""


def test_the_range_is_that_of_the_workload_s_python():
    only_new = table(cell("3.13", "langchain", "0.3", steps=True), cell("3.13", "langchain", "1", steps=True))
    only_new["cells"].append(cell("3.14", "langchain", "0.3", started=False, traced=False, steps=False))
    only_new["cells"].append(cell("3.14", "langchain", "1", steps=True))
    assert why(only_new, "langchain", "0.3.30", python="3.13") == ""
    assert why(only_new, "langchain", "0.3.30", python="3.14") == (
        "Impact AI traces langchain 1; this workload runs langchain 0.3.30"
    )


def test_a_library_that_versions_by_minor_is_ranged_by_minor():
    agents = table(cell("3.12", "openai-agents", "0.22", steps=True))
    assert why(agents, "openai-agents", "0.22.9") == ""
    assert why(agents, "openai-agents", "0.23.0") == (
        "Impact AI traces openai-agents 0.22; this workload runs openai-agents 0.23.0"
    )


def test_a_python_without_a_passing_cell_reads_the_pythons_impact_ai_traces_the_library_on():
    crewai = table(*[cell(python, "crewai", "1", steps=True) for python in ("3.11", "3.12", "3.13")])
    assert why(crewai, "crewai", "1.15.35", python="3.14") == (
        "Impact AI traces crewai on Python 3.11 to 3.13; this workload runs 3.14"
    )
    assert why(crewai, "crewai", "1.15.35", python="3.13") == ""


def test_a_failing_cell_proves_nothing():
    failing = table(cell("3.12", "openai", "3"), cell("3.12", "openai", "4", traced=False))
    assert why(failing, "openai", "4.1.0") == "Impact AI traces openai 3; this workload runs openai 4.1.0"
    no_steps = table(cell("3.12", "langgraph", "1", steps=False))
    assert why(no_steps, "langgraph", "1.2.19") is None


def test_a_library_with_no_passing_cell_at_all_is_untraced_with_nothing_to_name():
    assert why(OPENAI, "anthropic", "1.17.0") is None
    assert why(table(), "openai", "3.42.1") is None


def test_an_excluded_release_inside_the_proven_range_is_not_traced_and_says_so():
    excluded = table(cell("3.12", "openai", "3"), excluded=[{"library": "openai", "version": "3.43.0", "why": "x"}])
    assert (
        why(excluded, "openai", "3.43.0") == "Impact AI traces openai 3 except 3.43.0; this workload runs openai 3.43.0"
    )
    assert why(excluded, "openai", "3.43.1") == ""


def test_a_library_without_a_version_is_not_proven():
    assert why(OPENAI, "openai", None) == "Impact AI traces openai 4 to 7; this workload runs openai without a version"


# --- which instrumentations are armed ------------------------------------------------------------------------------


def test_the_instrumentations_of_ai_libraries_are_decided_by_the_cells_of_the_libraries_they_trace():
    assert qualification.gated() == {
        "anthropic": ("anthropic",),
        "boto3": ("botocore",),  # Bedrock's Converse is a botocore call; botocore is not listed as an AI library
        "google-genai": ("google-genai",),  # the Gemini API and Vertex AI, through Google's SDK
        "langchain": ("langchain", "langchain-core", "langgraph"),
        "openai": ("openai",),
        "openai_agents": ("openai-agents",),
    }


EVERYTHING = table(
    cell("3.12", "openai", "3"),
    cell("3.12", "anthropic", "1"),
    cell("3.12", "botocore", "1"),
    cell("3.12", "langchain", "1", steps=True),
    cell("3.12", "langchain-core", "1"),
    cell("3.12", "langgraph", "1", steps=True),
)


def screen(names, dists, of=EVERYTHING, python="3.12"):
    return qualification.screen(names, dists, table=of, python=python)


def test_what_a_cell_proves_is_armed_and_what_none_proves_is_not_with_its_reason():
    names = ["anthropic", "boto3", "fastapi", "openai"]
    dists = {"openai": "4.0.0", "anthropic": "1.17.0", "botocore": "1.43.123", "fastapi": "0.162.0"}
    refused, reasons = screen(names, dists)
    assert refused == {"openai"}
    assert reasons == {"openai": "Impact AI traces openai 3; this workload runs openai 4.0.0"}


def test_the_web_frameworks_keep_to_their_own_ranges():
    assert screen(["fastapi", "flask", "starlette"], {"fastapi": "0.162.0", "flask": "3.1.3"}) == (set(), {})


def test_one_instrumentation_of_several_libraries_is_armed_only_when_each_of_them_the_workload_has_is_proven():
    both = {"langchain-core": "1.6.13", "langgraph": "1.2.19"}
    assert screen(["langchain"], both) == (set(), {})
    newer = {"langchain-core": "1.6.13", "langgraph": "2.0.0", "langchain": "1.4.7"}
    assert screen(["langchain"], newer) == (
        {"langchain"},
        {"langchain": "Impact AI traces langgraph 1; this workload runs langgraph 2.0.0"},
    )


def test_bedrock_is_decided_by_botocore():
    assert screen(["boto3"], {"boto3": "1.43.123", "botocore": "1.43.123"}) == (set(), {})
    refused, reasons = screen(["boto3"], {"boto3": "2.0.0", "botocore": "2.0.0"})
    assert refused == {"boto3"}
    assert reasons["boto3"] == "Impact AI traces botocore 1; this workload runs botocore 2.0.0"


def test_google_genai_is_decided_by_its_cells_and_3_waits_for_one():
    cells = table(cell("3.12", "google-genai", "1"), cell("3.12", "google-genai", "2"))
    assert screen(["google-genai"], {"google-genai": "2.34.0"}, of=cells) == (set(), {})
    assert screen(["google-genai"], {"google-genai": "1.75.0"}, of=cells) == (set(), {})
    assert screen(["google-genai"], {"google-genai": "3.0.0"}, of=cells) == (
        {"google-genai"},
        {"google-genai": "Impact AI traces google-genai 1 to 2; this workload runs google-genai 3.0.0"},
    )


def test_a_library_with_no_passing_cell_is_not_armed_and_has_no_line():
    refused, reasons = screen(["openai_agents"], {"openai-agents": "0.22.3"})
    assert (refused, reasons) == ({"openai_agents"}, {})


def test_a_process_without_an_ai_library_never_reads_the_table(monkeypatch):
    def unread():
        raise AssertionError("the table was read")

    monkeypatch.setattr(qualification, "load", unread)
    assert qualification.screen(["openai", "fastapi"], {"fastapi": "0.162.0"}) == (set(), {})


def test_another_python_is_named_with_the_pythons_that_are_proven():
    refused, reasons = screen(["anthropic"], {"anthropic": "1.17.0"}, python="3.14")
    assert refused == {"anthropic"}
    assert reasons == {"anthropic": "Impact AI traces anthropic on Python 3.12; this workload runs 3.14"}


def test_only_the_matrix_s_switch_arms_what_no_cell_proves():
    assert qualification.arms_unproven({qualification.MATRIX: "1"})
    for environ in (
        {},
        {qualification.MATRIX: ""},
        {qualification.MATRIX: "0"},
        {"IMPACT_PYTHON_QUALIFICATION": "off"},
    ):
        assert not qualification.arms_unproven(environ), environ


def test_what_no_cell_proves_is_refused_and_named_whatever_the_switch_says():
    # The switch decides what is armed, never what the report says: screen() does not read it.
    assert "environ" not in qualification.screen.__code__.co_varnames
    refused, reasons = screen(["openai"], {"openai": "9.0.0"})
    assert refused == {"openai"}
    assert reasons == {"openai": "Impact AI traces openai 3; this workload runs openai 9.0.0"}


def test_a_qualification_that_cannot_be_read_proves_nothing_and_names_the_type_of_the_error(monkeypatch):
    def broken():
        raise ValueError("a message that might hold anything")

    monkeypatch.setattr(qualification, "load", broken)
    refused, reasons = qualification.screen(["openai", "fastapi"], {"openai": "3.42.1"})
    assert refused == {"openai"}
    assert reasons == {"openai": "Impact AI could not read this image's qualification (ValueError)"}


def test_a_source_tree_has_no_generated_table():
    with pytest.raises(ImportError):
        qualification.load()


def test_the_python_of_the_process_is_its_major_and_minor_version():
    assert qualification.python_version() == f"{sys.version_info.major}.{sys.version_info.minor}"


# --- the committed table, as the package reads it ------------------------------------------------------------------


def committed() -> dict:
    import json

    return json.loads((ROOT / "python" / "qualification.json").read_text())


def test_every_library_the_package_decides_by_cells_has_cells_and_every_cell_is_of_one():
    libraries = {library for names in qualification.gated().values() for library in names}
    in_table = {c["library"] for c in committed()["cells"]}
    assert in_table == libraries


# The series each library was traced in before the qualification existed (kit 0.3.0-rc1, whose instrumentations armed by
# their own ranges: openai >=1.26,<4, anthropic >=0.51,<3, openai-agents >=0.3.3,<1, langchain >=0.3.22,<2 or
# langchain-core >=0.3.49,<2, LangGraph through langchain-core, botocore any), from the floor to the newest series
# released when these cells were measured. Each must stay inside the proven range on every Python, unless it is listed
# in DROPPED with the reason.
TRACED_BEFORE = {
    "openai": ("1.26.0", "2.54.0", "3.48.0"),
    "anthropic": ("0.51.0", "0.80.0", "0.125.0", "1.17.0", "2.7.0"),
    "openai-agents": ("0.3.3", "0.10.5", "0.22.3", "0.23.1", "0.24.0"),
    "langchain": ("0.3.22", "0.3.30", "1.4.9"),
    "langchain-core": ("0.3.49", "0.3.86", "1.7.6"),
    "langgraph": ("0.2.76", "0.3.34", "0.6.11", "1.2.20"),
    "botocore": ("1.43.123",),
}
DROPPED = {
    ("langchain", "0.3.22", "3.14"): "LangChain 0.3 does not import on Python 3.14, with the kit or without it",
    ("langchain", "0.3.30", "3.14"): "LangChain 0.3 does not import on Python 3.14, with the kit or without it",
}


def test_every_series_traced_before_the_qualification_is_still_traced_on_every_python():
    lost = [
        (library, version, python, why(committed(), library, version, python=python))
        for library, versions in TRACED_BEFORE.items()
        for version in versions
        for python in qualify_python.PYTHONS
        if (library, version, python) not in DROPPED and why(committed(), library, version, python=python) != ""
    ]
    assert lost == []
    for library, version, python in DROPPED:
        assert why(committed(), library, version, python=python) != "", "a dropped series that is traced again"


def test_with_the_committed_table_the_fixtures_libraries_are_traced_where_their_cells_pass():
    table = committed()
    passing = {(c["python"], c["library"], c["major"]) for c in table["cells"] if qualification.passes(c)}
    for variant in qualify_python.VARIANTS.values():
        version = qualify_python.pinned(variant.fixture, variant.library)
        for python in qualify_python.PYTHONS:
            expected = (python, variant.library, qualify_python.series(version)) in passing
            assert (why(table, variant.library, version, python=python) == "") == expected, (python, variant)


def test_the_script_and_the_package_range_series_alike():
    cases = [({"1", "3"}, set()), ({"1", "3"}, {"2"}), ({"0.3", "0.24"}, {"0.10"}), ({"0.2", "1"}, {"0.3", "2"})]
    for passing, failing in cases:
        assert qualify_python.segments(passing, failing) == qualification.segments(passing, failing)


# The pattern impact.ai reads a range reason with (Cover your AI's setup reasons): what Impact AI traces, then what the
# workload runs, in letters, digits, spaces and ._+- only.
SERVICE_RANGE_REASON = re.compile(
    r"^Impact AI traces ([A-Za-z0-9._+-][A-Za-z0-9 ._+-]{0,79}); "
    r"this workload runs ([A-Za-z0-9._+-][A-Za-z0-9 ._+-]{0,59})$"
)


def kit_sentences() -> list[str]:
    """Every form of range reason the package can say."""
    from impact_instrumentation import runtime

    broken = table(cell("3.12", "openai", "1"), cell("3.12", "openai", "2", traced=False), cell("3.12", "openai", "3"))
    gaps = table(*[cell("3.12", "openai", major) for major in ("1", "3", "5", "7")])
    gaps["cells"] += [cell("3.12", "openai", major, traced=False) for major in ("2", "4", "6")]
    pythons = table(*[cell(python, "crewai", "1", steps=True) for python in ("3.11", "3.13", "3.14")])
    excluded = table(cell("3.12", "openai", "3"), excluded=[{"library": "openai", "version": "3.43.0", "why": "x"}])
    langchain = runtime.Spec(
        "langchain",
        "m",
        "C",
        ("langchain",),
        (),
        (("langchain", "<2,>=0.3.22", "0.3.10"), ("langchain-core", "<2,>=0.3.49", "0.3.40")),
    )
    return [
        why(OPENAI, "openai", "8.0.0"),
        why(OPENAI, "openai", None),
        why(broken, "openai", "2.54.0"),
        why(gaps, "openai", "8.0.0"),
        why(pythons, "crewai", "1.15.35", python="3.12"),
        why(excluded, "openai", "3.43.0"),
        why(
            table(cell("3.12", "openai-agents", "0.3", steps=True), cell("3.12", "openai-agents", "0.24", steps=True)),
            "openai-agents",
            "0.25.0rc1",
        ),  # fmt: skip
        runtime.unsupported(langchain),
        *[
            f"Impact AI traces x {runtime.spoken_specifier(specifier)}; this workload runs x 9.9.9"
            for specifier in ("<4,>=1.26.0", "~=0.92", ">=2.0", "<2.5.1", "<=2.5", ">=1.0,!=1.4.0,<3", "==1.2.3", "")
        ],
    ]


def test_every_range_reason_the_package_says_is_one_impact_ai_reads():
    for sentence in kit_sentences():
        assert SERVICE_RANGE_REASON.match(sentence), sentence

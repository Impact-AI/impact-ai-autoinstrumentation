# SPDX-License-Identifier: Apache-2.0

"""The registry of instrumentations is generated when the payload is built, so that no process has to read package
metadata at start (importing importlib.metadata alone costs ~200 ms)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import assemble_payload as assemble  # noqa: E402


def instrumentation(root: Path, name: str, cls: str, requires: list[str], entry: str | None = None) -> None:
    info = root / f"opentelemetry_instrumentation_{name}-1.0.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: opentelemetry-instrumentation-{name}", "Version: 1.0"]
    lines += [f"Requires-Dist: {r}" for r in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n")
    (info / "entry_points.txt").write_text(f"[opentelemetry_instrumentor]\n{entry or name} = {name}_instr:{cls}\n")


def generated(root: Path):
    namespace: dict = {}
    exec(assemble.generate_registry(root), namespace)  # noqa: S102 - the generator's own output
    return namespace["INSTRUMENTATIONS"]


def test_each_instrumentation_lists_its_library_requirements(tmp_path):
    instrumentation(tmp_path, "openai", "OpenAIInstrumentor", ['openai<4,>=1.26.0; extra == "instruments"'])
    assert generated(tmp_path) == (("openai", "openai_instr", "OpenAIInstrumentor", (("openai", "<4,>=1.26.0"),), ()),)


def test_old_style_parenthesised_specifiers_and_spaces_are_normalised(tmp_path):
    instrumentation(tmp_path, "a", "A", ['boto3 (>= 1.28.57); extra == "instruments"'])
    instrumentation(tmp_path, "b", "B", ['anthropic >= 0.51.0, < 2; extra == "instruments"'])
    got = {entry[0]: entry[3] for entry in generated(tmp_path)}
    assert got == {"a": (("boto3", ">=1.28.57"),), "b": (("anthropic", ">=0.51.0,<2"),)}


def test_requirements_without_a_specifier_are_kept(tmp_path):
    instrumentation(tmp_path, "c", "C", ['pyramid; extra == "instruments"'])
    assert generated(tmp_path)[0][3] == (("pyramid", ""),)


def test_any_of_requirements_are_separate(tmp_path):
    instrumentation(
        tmp_path, "d", "D", ['psycopg>=3; extra == "instruments-any"', 'psycopg-binary>=3; extra == "instruments-any"']
    )
    (entry,) = generated(tmp_path)
    assert entry[3] == ()
    assert entry[4] == (("psycopg", ">=3"), ("psycopg-binary", ">=3"))


def test_dependencies_that_are_not_instrumented_libraries_are_ignored(tmp_path):
    instrumentation(
        tmp_path,
        "e",
        "E",
        ["opentelemetry-api~=1.12", 'wrapt<2; python_version < "3.11"', 'flask>=1.0; extra == "instruments"'],
    )
    assert generated(tmp_path)[0][3] == (("flask", ">=1.0"),)


def test_distributions_without_instrumentors_are_left_out(tmp_path):
    info = tmp_path / "somelib-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: somelib\nVersion: 1.0\n")
    assert generated(tmp_path) == ()


def test_the_output_is_sorted_and_stable(tmp_path):
    for name in ("zeta", "alpha", "mid"):
        instrumentation(tmp_path, name, name.title(), [f'{name}lib; extra == "instruments"'])
    first = assemble.generate_registry(tmp_path)
    assert [e[0] for e in generated(tmp_path)] == ["alpha", "mid", "zeta"]
    assert assemble.generate_registry(tmp_path) == first


@pytest.mark.parametrize("bad", ["not a requirement ;;", ""])
def test_unparseable_lines_are_skipped_not_fatal(tmp_path, bad):
    instrumentation(
        tmp_path, "f", "F", [bad, 'flask>=1; extra == "instruments"'] if bad else ['flask>=1; extra == "instruments"']
    )
    assert generated(tmp_path)[0][3] == (("flask", ">=1"),)

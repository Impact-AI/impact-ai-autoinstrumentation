# SPDX-License-Identifier: Apache-2.0

"""What the image's qualification proved: an AI library is traced only where a passing cell matches the workload.

The qualification matrix (python/tests/image/test_python_cells.py) runs a fixture workload per cell, with the payload
injected and the mock providers, and writes its answers to python/qualification.json. A cell is one Python version, one
library and one major version of it (the minor, for a library that versions by minor: 0.22 of openai-agents 0.22.3):
did the workload start and serve, did each model call arrive as one span with its provider, model and tokens, where did
its content arrive, and for an agent framework, did its agent and tool steps arrive. The build puts the file at the root
of the payload, and the same table in _qualification.py beside this package, which a start imports from its bytecode
instead of parsing JSON (as it does the registry of instrumentations).

An instrumentation of an AI library is armed only where the qualification proves the workload's Python and the version
of each library it traces that the workload has. A library's proven range on a Python runs from its oldest passing cell
to its newest: a release inside it is traced without a cell of its own, one above the newest or below the oldest is
not, nor is one of a series whose cell fails or a release the qualification excludes. Nothing of an instrumentation that
is not proven is armed, so the workload starts as it would without the kit for those libraries, the setup report lists
them as untraced, and its skipped line says what Impact AI traces and what the workload runs, in that order, as the
Node.js package says it of an older Node:

    openai: Impact AI traces openai 1 to 3; this workload runs openai 4.0.0
    langchain: Impact AI traces langgraph on Python 3.11 to 3.13; this workload runs 3.14

A library with no passing cell at all is untraced and has no skipped line: there is no range to name. The web
frameworks' instrumentations, which make the request a model call hangs under, are not AI libraries and keep to their
own version ranges.

The qualification matrix sets IMPACT_QUALIFICATION_MATRIX=1 in its fixture workloads, which arms the instrumentations
of AI libraries by their own version ranges alone, so that it finds what works whatever the file says. The setup report
still takes what is traced from the cells alone. Standard library only, and read at start, before the application
runs: a directory listing gave the versions, so nothing is imported to know them.
"""

from __future__ import annotations

import sys
from collections.abc import Collection, Mapping

from impact_instrumentation import frameworks

SCHEMA = "impact.python.qualification.v1"
# Set by the qualification matrix only (python/tests/image/test_python_cells.py): arm unproven libraries, report
# them as untraced all the same.
MATRIX = "IMPACT_QUALIFICATION_MATRIX"

# Instrumentations of AI libraries whose library the setup report does not list: OpenLLMetry's Bedrock instrumentation
# traces Converse, a call of botocore, which is in every workload that has boto3 and says nothing about AI by itself.
UNLISTED = {"boto3": ("botocore",)}


def gated() -> dict[str, tuple[str, ...]]:
    """instrumentation -> the libraries whose cells decide whether it is armed: the catalogue's, and UNLISTED."""
    found: dict[str, list[str]] = {}
    for name, entry in frameworks.CATALOG.items():
        if entry.tracer is not None:
            found.setdefault(entry.tracer, []).append(name)
    for tracer, names in UNLISTED.items():
        found.setdefault(tracer, []).extend(names)
    return {tracer: tuple(sorted(names)) for tracer, names in found.items()}


def load() -> Mapping:
    """The table the build generated from qualification.json: ImportError in a source tree, which has none, and
    ValueError for a table that is not a qualification."""
    from impact_instrumentation import _qualification

    table = _qualification.TABLE
    if not isinstance(table, Mapping) or table.get("schema") != SCHEMA or not isinstance(table.get("cells"), list):
        raise ValueError(f"not a {SCHEMA}")
    return table


def _leading_number(text: str) -> int | None:
    digits = ""
    for character in text:
        if not character.isdigit():
            break
        digits += character
    return int(digits) if digits else None


def series(version: str | None) -> str | None:
    """What a cell of this version proves: its major version (3 of 3.42.1), or for a 0.x version its minor (0.22 of
    0.22.3). None when the version does not start with a number."""
    parts = (version or "").strip().split(".")
    major = _leading_number(parts[0])
    if major is None:
        return None
    minor = _leading_number(parts[1]) if major == 0 and len(parts) > 1 else None
    return f"0.{minor}" if minor is not None else str(major)


def _number(text: str) -> tuple[int, ...]:
    return tuple(_leading_number(part) or 0 for part in str(text).split("."))


def passes(cell: Mapping) -> bool:
    """A cell that proves its library: the workload started and served, the model calls arrived as spans, and an agent
    framework's steps arrived too (steps is None for a library that is not one)."""
    return cell.get("started") is True and cell.get("traced") is True and cell.get("steps") is not False


def _follows(previous: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """The next Python (3.12 after 3.11)."""
    return len(previous) == len(current) and previous[:-1] == current[:-1] and current[-1] == previous[-1] + 1


def _said(run: list[str]) -> str:
    return run[0] if len(run) == 1 else f"{run[0]} to {run[-1]}"


def segments(passing: Collection[str], failing: Collection[str] = ()) -> list[list[str]]:
    """The proven range of a library's series on one Python: from its oldest passing cell to its newest, broken where a
    cell between them fails. [['1', '3']] for cells of 1 and 3; [['1'], ['3']] when a cell of 2 fails."""
    failed = set(failing) - set(passing)
    runs: list[list[str]] = []
    broken = True
    for value in sorted(set(passing) | failed, key=_number):
        if value in failed:
            broken = True
        elif broken:
            runs.append([value])
            broken = False
        else:
            runs[-1].append(value)
    return runs


def inside(value: str | None, runs: list[list[str]]) -> bool:
    """Whether a series is inside a proven range (segments)."""
    if value is None:
        return False
    return any(_number(run[0]) <= _number(value) <= _number(run[-1]) for run in runs)


def spoken(passing: Collection[str], failing: Collection[str] = ()) -> str:
    """A proven range as a sentence says it, with no comma: '4 to 7', '0.22', '1 and 3' (2 fails), '0.3 to 1'."""
    return " and ".join(_said(run) for run in segments(passing, failing))


def spoken_pythons(pythons: Collection[str]) -> str:
    """Pythons as a sentence says them, with no comma: '3.11 to 3.13', '3.12', '3.11 and 3.13 to 3.14'."""
    runs: list[list[str]] = []
    for value in sorted(set(pythons), key=_number):
        if runs and _follows(_number(runs[-1][-1]), _number(value)):
            runs[-1].append(value)
        else:
            runs.append([value])
    return " and ".join(_said(run) for run in runs)


def python_version() -> str:
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def _cells(table: Mapping, library: str) -> list[Mapping]:
    return [c for c in table.get("cells", []) if isinstance(c, Mapping) and c.get("library") == library]


def proven(table: Mapping, *, python: str, library: str) -> str:
    """What Impact AI traces of `library` on `python`, as the report says it ('openai 1 to 3'); '' for nothing."""
    cells = [c for c in _cells(table, library) if c.get("python") == python]
    runs = spoken({str(c.get("major")) for c in cells if passes(c)}, {str(c.get("major")) for c in cells})
    return f"{library} {runs}" if runs else ""


def why_not(table: Mapping, *, python: str, library: str, version: str | None) -> str | None:
    """'' when the qualification proves `library` at `version` on `python`; otherwise what Impact AI traces and what the
    workload runs; None when no cell of the library passes at all (it is untraced, with nothing to name)."""
    cells = _cells(table, library)
    if not any(passes(c) for c in cells):
        return None
    here = [c for c in cells if c.get("python") == python]
    passing = {str(c.get("major")) for c in here if passes(c)}
    if not passing:
        pythons = spoken_pythons({str(c.get("python")) for c in cells if passes(c)})
        return f"Impact AI traces {library} on Python {pythons}; this workload runs {python}"
    runs = f"{library} {version}" if version else f"{library} without a version"
    excluded = {
        str(entry.get("version"))
        for entry in table.get("excluded", [])
        if isinstance(entry, Mapping) and entry.get("library") == library
    }
    traced = proven(table, python=python, library=library)
    if version in excluded:
        return f"Impact AI traces {traced} except {version}; this workload runs {runs}"
    if inside(series(version), segments(passing, {str(c.get("major")) for c in here})):
        return ""
    return f"Impact AI traces {traced}; this workload runs {runs}"


def screen(
    names: Collection[str],
    dists: Mapping[str, str | None],
    *,
    table: Mapping | None = None,
    python: str | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Which of the planned instrumentations (by name) the qualification does not prove for this workload, whatever
    IMPACT_QUALIFICATION_MATRIX says (arms_unproven decides only what is armed).

    Returns (the instrumentations not to arm, {instrumentation: what Impact AI traces and what the workload runs}); the
    second holds those with something to name. dists: {normalized distribution name: version}, as detect.scan lists
    them. The table is the payload's (load) when it is not given; one that cannot be read proves nothing, and the reason
    says so with the type of the error, never its message.
    """
    rules = gated()
    # Only the instrumentations of AI libraries the workload has: a process with none never reads the table.
    wanted = [name for name in names if name in rules and any(library in dists for library in rules[name])]
    if not wanted:
        return set(), {}
    if table is None:
        try:
            table = load()
        except Exception as error:  # noqa: BLE001 - a broken file must not stop the start: it proves nothing
            why = f"Impact AI could not read this image's qualification ({type(error).__name__})"
            return set(wanted), dict.fromkeys(wanted, why)
    python = python or python_version()
    refused: set[str] = set()
    reasons: dict[str, str] = {}
    for name in wanted:
        present = [library for library in rules[name] if library in dists]
        answers = [why_not(table, python=python, library=library, version=dists[library]) for library in present]
        if all(answer == "" for answer in answers):
            continue
        refused.add(name)
        named = [answer for answer in answers if answer]
        if named:
            reasons[name] = named[0]
    return refused, reasons


def arms_unproven(environ: Mapping[str, str]) -> bool:
    """The qualification matrix's fixture workloads arm what the qualification does not prove (its own cells find out
    what works); the setup report still lists it as untraced."""
    return (environ.get(MATRIX) or "").strip() == "1"

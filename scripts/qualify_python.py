#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The qualification of the Python image: its matrix, python/qualification.json, and the cells a change touches.

    python3 scripts/qualify_python.py libraries                   every library of the matrix
    python3 scripts/qualify_python.py select                      the cells this change touches: `library` (on every
                                                                  Python) or `library@python`
    python3 scripts/qualify_python.py fixtures [SELECTION ...]    the fixture images their cells run in: "kind python"
    python3 scripts/qualify_python.py fixtures --newest           the fixtures of the weekly run's cells (each library's
                                                                  newest series)
    python3 scripts/qualify_python.py upgrades FIXTURE            what the weekly run upgrades in a fixture
    python3 scripts/qualify_python.py report DIRECTORY            the weekly run's summary; fails when a release fails
                                                                  its cell or needs one
    python3 scripts/qualify_python.py assemble DIRECTORY FILE     write the qualification from the cells measured

A cell is one Python version, one AI library and one major version of it (the minor, for a library that versions by
minor: openai-agents 0.24). Each library has a cell at the newest series it is traced in and one at the oldest (the
fixture "oldest"), and the package traces what lies between them. The matrix (python/tests/image/test_python_cells.py)
runs a fixture workload per cell with the payload injected and the mock providers, and asks: did the workload start
and serve; did each model call arrive as one span with its provider, model and tokens; where did its content arrive, on
the span or on a span event; and, for an agent framework, did its agent and tool steps arrive. The package traces a
library only where the passing cells of the workload's Python prove the library's version: from the oldest passing cell
to the newest, broken where a cell fails (src/impact_instrumentation/qualification.py).

`make qualify-python` runs every cell with QUALIFY_DIR set, which makes each one write its answer to a file of its own
instead of comparing it with the committed file, and then assembles them here: sorted, so that a change of one cell
shows as a small diff, and with the file's list of excluded releases kept. `select` reads the change from git: the
first parent of the commit CI checks out (the base of a pull request's merge commit, or the previous commit of a push),
else the merge base with origin/main, or CELLS_BASE; CELLS=all|none|<library>[@<python>],... replaces it. A change of a
library's own inputs runs its cells on every Python; a change of what every cell runs on (the package, say) runs every
library on CANARY_PYTHON, and the nightly and release runs take every cell. Standard library only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "python" / "qualification.json"
APPS = ROOT / "testing" / "apps"
SCHEMA = "impact.python.qualification.v1"
FIELDS = ("python", "library", "major", "version", "setting", "started", "traced", "content", "steps")
PYTHONS = ("3.11", "3.12", "3.13", "3.14")
# The Python a change of what every cell runs on is checked on in a pull request (the end-to-end job builds its
# fixtures anyway); the nightly and release runs take every Python.
CANARY_PYTHON = "3.12"
# The weekly run upgrades a fixture's libraries to their newest releases; botocore through boto3, which pins it.
NEWEST_THROUGH = {"botocore": "boto3"}

# The content setting each library ran with: OpenTelemetry's GenAI packages read the kit's default, OpenLLMetry's
# Bedrock package its own switch, which is on unless the workload asks for no content.
GENAI = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY"
TRACELOOP = "TRACELOOP_TRACE_CONTENT=true"

Variant = namedtuple("Variant", ["library", "fixture", "app", "requests", "calls", "agent", "setting"])
# library   the distribution the cell is about, by its PEP 503 name
# fixture   the fixture image, fixture-app[-<fixture>]:<python>, built from testing/apps/requirements[-<fixture>].txt
# app       the module uvicorn serves (testing/apps/<app>.py)
# requests  the endpoints the cell asks once each, each answered with the stub's answer
# calls     the model calls they make together: each must arrive as exactly one span
# agent     an agent framework: its agent and tool steps must arrive too
# setting   the content setting the library ran with
# Google's SDK: the Gemini API and Vertex AI mode, each sync, async, streaming and async streaming
# (app_google_genai.py).
GOOGLE_GENAI = tuple(
    f"/chat/google-genai/{api}/{call}"
    for api in ("gemini", "vertex")
    for call in ("sync", "async", "stream", "async-stream")
)
VARIANTS = {
    "openai-3": Variant("openai", "", "app_asgi", ("/chat/openai",), 1, False, GENAI),
    "anthropic-2": Variant("anthropic", "", "app_asgi", ("/chat/anthropic",), 1, False, GENAI),
    "botocore-1": Variant("botocore", "", "app_asgi", ("/chat/bedrock",), 1, False, TRACELOOP),
    "openai-agents-0.24": Variant("openai-agents", "agents", "app_agents", ("/agent/openai-agents",), 2, True, GENAI),
    "langchain-1": Variant("langchain", "agents", "app_agents", ("/agent/langchain",), 2, True, GENAI),
    "langchain-core-1": Variant("langchain-core", "agents", "app_agents", ("/chain/langchain-core",), 1, False, GENAI),
    "langgraph-1": Variant("langgraph", "agents", "app_agents", ("/agent/langgraph",), 2, True, GENAI),
    # A library that joined the traced set with its cells: one at its newest series, one at its instrumentation's floor
    # (google-genai 1.32.0), each in a fixture of its own so that a change to one library's pins touches no other's.
    "google-genai-2": Variant(
        "google-genai", "google-genai", "app_google_genai", GOOGLE_GENAI, len(GOOGLE_GENAI), False, GENAI
    ),
    "google-genai-1": Variant(
        "google-genai", "google-genai-oldest", "app_google_genai", GOOGLE_GENAI, len(GOOGLE_GENAI), False, GENAI
    ),
    # The oldest series each library was traced in before the qualification existed, at the floor of its
    # instrumentation's range: the qualification's range runs from these cells to the ones above.
    "openai-1": Variant("openai", "oldest", "app_asgi", ("/chat/openai",), 1, False, GENAI),
    "anthropic-0.51": Variant("anthropic", "oldest", "app_asgi", ("/chat/anthropic",), 1, False, GENAI),
    "openai-agents-0.3": Variant("openai-agents", "oldest", "app_agents", ("/agent/openai-agents",), 2, True, GENAI),
    "langchain-0.3": Variant("langchain", "oldest", "app_agents", ("/agent/langchain-executor",), 2, True, GENAI),
    "langchain-core-0.3": Variant(
        "langchain-core", "oldest", "app_agents", ("/chain/langchain-core",), 1, False, GENAI
    ),
    "langgraph-0.2": Variant("langgraph", "oldest", "app_agents", ("/agent/langgraph",), 2, True, GENAI),
}
LIBRARIES = tuple(sorted({variant.library for variant in VARIANTS.values()}))

# The payload's packages that trace one library or a few. A change to any other package of the payload (the SDK,
# util-genai, the web frameworks' instrumentations the fixtures run under) touches every cell.
INSTRUMENTATIONS = {
    "opentelemetry-instrumentation-genai-openai": ("openai",),
    "opentelemetry-instrumentation-genai-anthropic": ("anthropic",),
    "opentelemetry-instrumentation-bedrock": ("botocore",),
    "opentelemetry-instrumentation-genai-openai-agents": ("openai-agents",),
    "opentelemetry-instrumentation-genai-langchain": ("langchain", "langchain-core", "langgraph"),
    "opentelemetry-instrumentation-google-genai": ("google-genai",),
}
# Files every cell runs on: the package, the payload's build, the mock providers, the fixtures' image and the matrix.
EVERY_CELL = (
    "python/src/*",
    "python/Dockerfile",
    "python/.dockerignore",
    "python/tools/assemble_payload.py",
    "python/tests/image/conftest.py",
    "python/tests/image/containers.py",
    "python/tests/image/test_python_cells.py",
    "scripts/qualify_python.py",
    "scripts/python-cells.sh",
    "testing/mock/*",
    "testing/apps/Dockerfile",
    "testing/apps/.dockerignore",
)


def requirements(fixture: str) -> str:
    """The pins of a fixture image, relative to testing/apps."""
    return f"requirements{'-' + fixture if fixture else ''}.txt"


def series(version: str | None) -> str | None:
    """What a cell of this version proves: the major version (3 of 3.42.1), or the minor of a 0.x version (0.22 of
    0.22.3). The same rule as the package's (impact_instrumentation.qualification.series)."""
    match = re.match(r"(\d+)(?:\.(\d+))?", version or "")
    if not match:
        return None
    major = int(match[1])
    if major == 0 and match[2] is not None:
        return f"0.{int(match[2])}"
    return str(major)


def number(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def key(cell: dict) -> tuple:
    return (number(cell["python"]), cell["library"], number(cell["major"]))


def pins(text: str | None) -> dict[str, str]:
    """name -> version of a requirements or lock file (`name==version`, markers and hashes ignored)."""
    found = {}
    for line in (text or "").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line.strip())
        if match:
            found[re.sub(r"[-_.]+", "-", match[1]).lower()] = match[2]
    return found


def pinned(fixture: str, library: str) -> str | None:
    """The version of `library` the fixture image installs."""
    return pins((APPS / requirements(fixture)).read_text()).get(library)


# --- the qualification file -----------------------------------------------------------------------------------------


def check(table: dict) -> None:
    """Refuse a table that is not in the form of the schema."""
    if not isinstance(table, dict) or table.get("schema") != SCHEMA:
        raise SystemExit(f"not a qualification of the schema {SCHEMA}")
    for cell in table.get("cells", []):
        if tuple(cell) != FIELDS:
            raise SystemExit(f"a cell with the fields {list(cell)}, expected {list(FIELDS)}: {cell}")
    keys = [key(cell) for cell in table["cells"]]
    if len(keys) != len(set(keys)):
        raise SystemExit("two cells for the same Python, library and major version")
    for entry in table.get("excluded", []):
        if set(entry) != {"library", "version", "why"}:
            raise SystemExit(f"an excluded release names its library, its version and why: {entry}")


def assemble(directory: Path, committed: dict | None = None) -> dict:
    """The table of the cells measured in `directory`, keeping the excluded releases of the committed one."""
    cells = [json.loads(path.read_text()) for path in sorted(Path(directory).glob("*.json"))]
    if not cells:
        raise SystemExit(f"no cells in {directory}: run the matrix first (make qualify-python)")
    cells = [{field: cell[field] for field in FIELDS} if set(cell) == set(FIELDS) else cell for cell in cells]
    table = {
        "schema": SCHEMA,
        "cells": sorted(cells, key=key),
        "excluded": list((committed or {}).get("excluded", [])),
    }
    check(table)
    return table


def write(table: dict, path: Path) -> None:
    path.write_text(json.dumps(table, indent=2) + "\n")


# --- the cells a change touches -------------------------------------------------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - git with arguments of this script
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def base_revision(environ=os.environ) -> str:
    """The commit the change is measured against."""
    if environ.get("CELLS_BASE"):
        return environ["CELLS_BASE"]
    if environ.get("GITHUB_ACTIONS") == "true":
        # A pull request is checked out as the merge of its head into its base, a push as the pushed commit: either
        # way the first parent is what the change is made on. The checkout is shallow, so the parent is fetched.
        parents = re.findall(r"^parent ([0-9a-f]{40})$", git("cat-file", "commit", "HEAD"), re.MULTILINE)
        if not parents:
            raise RuntimeError("the commit has no parent")
        try:
            git("cat-file", "-e", f"{parents[0]}^{{commit}}")
        except subprocess.CalledProcessError:
            git("fetch", "--no-tags", "--quiet", "--depth=1", "origin", parents[0])
        return parents[0]
    return git("merge-base", "HEAD", "origin/main").strip()


def content(revision: str | None, path: str) -> str | None:
    """A file at a revision, or in the working tree (revision None); None when it is not there."""
    if revision is None:
        file = ROOT / path
        return file.read_text() if file.is_file() else None
    try:
        return git("show", f"{revision}:{path}")
    except subprocess.CalledProcessError:
        return None


def cells_of(text: str | None) -> dict | None:
    """key -> cell, and the excluded releases by library, of a qualification's text; None when it is not one."""
    if text is None:
        return {"cells": {}, "excluded": {}}
    try:
        table = json.loads(text)
        cells = {(c["python"], c["library"], c["major"]): c for c in table["cells"]}
        excluded = {}
        for entry in table.get("excluded", []):
            excluded.setdefault(entry["library"], []).append(entry)
        return {"cells": cells, "excluded": excluded}
    except (ValueError, KeyError, TypeError):
        return None


def scope(path: str, before: str | None, after: str | None) -> tuple[set[str], set[str]]:
    """The libraries whose cells a change of one file touches: (on every Python, on CANARY_PYTHON only).

    A change of a library's own inputs (its cells or excluded releases, the package that traces it, its fixture) runs
    its cells on every Python. A change of what every cell runs on (the package, the payload's build and its other
    packages, the mock, the fixtures' image, the matrix) runs every library on CANARY_PYTHON: the end-to-end job of a
    pull request stays short, and the nightly and release runs take every Python.
    """
    every = set(LIBRARIES)
    if path == "python/qualification.json":
        old, new = cells_of(before), cells_of(after)
        if old is None or new is None:
            return every, set()
        changed = {
            k[1] for k in old["cells"].keys() | new["cells"].keys() if old["cells"].get(k) != new["cells"].get(k)
        }
        for library in old["excluded"].keys() | new["excluded"].keys():
            if old["excluded"].get(library) != new["excluded"].get(library):
                changed.add(library)
        return changed, set()
    if path == "python/requirements.lock":
        old, new = pins(before), pins(after)
        names = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
        own = {library for name in names if name in INSTRUMENTATIONS for library in INSTRUMENTATIONS[name]}
        shared = every if any(name not in INSTRUMENTATIONS for name in names) else set()
        return own, shared
    if any(fnmatch.fnmatch(path, pattern) for pattern in EVERY_CELL):
        return set(), every
    found = set()
    for variant in VARIANTS.values():
        if path in (f"testing/apps/{requirements(variant.fixture)}", f"testing/apps/{variant.app}.py"):
            found.add(variant.library)
    return found, set()


def touched(path: str, before: str | None, after: str | None) -> set[str]:
    """The libraries whose cells a change of one file touches, on any Python."""
    full, canary = scope(path, before, after)
    return full | canary


def parse(names: str) -> list[str]:
    """Selection tokens, `library` (every Python) or `library@python`, from a list split by commas or spaces."""
    tokens = [name.strip() for name in re.split(r"[,\s]+", names) if name.strip()]
    unknown = sorted({token.split("@")[0] for token in tokens} - set(LIBRARIES))
    if unknown:
        raise SystemExit(f"CELLS names libraries the matrix does not have: {unknown}; it has {list(LIBRARIES)}")
    odd = sorted(token for token in tokens if "@" in token and token.split("@", 1)[1] not in PYTHONS)
    if odd:
        raise SystemExit(f"CELLS names Pythons the matrix does not have: {odd}; it has {list(PYTHONS)}")
    return sorted(set(tokens))


def select(environ=os.environ) -> list[str]:
    """The cells this change touches, or those CELLS names: `library` for every Python, `library@python` for one."""
    wanted = (environ.get("CELLS") or "").strip()
    if wanted:
        if wanted == "all":
            return list(LIBRARIES)
        if wanted == "none":
            return []
        return parse(wanted)
    try:
        base = base_revision(environ)
        paths = [line for line in git("diff", "--name-only", base).splitlines() if line]
        paths += [line for line in git("ls-files", "--others", "--exclude-standard").splitlines() if line]
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"cannot tell what this change touches ({type(error).__name__}): every cell", file=sys.stderr)
        return list(LIBRARIES)
    full: set[str] = set()
    canary: set[str] = set()
    for path in sorted(set(paths)):
        own, shared = scope(path, content(base, path), content(None, path))
        full |= own
        canary |= shared
    return sorted(full & set(LIBRARIES)) + sorted(f"{name}@{CANARY_PYTHON}" for name in canary - full)


def newest() -> set[str]:
    """The variants the weekly run upgrades: each library's newest series, the cell its newest release is compared
    with. A library's older cells (the oldest fixture) stay at their pins."""
    top: dict[str, tuple[tuple[int, ...], str]] = {}
    for name, variant in VARIANTS.items():
        found = number(series(pinned(variant.fixture, variant.library)) or "0")
        if variant.library not in top or found > top[variant.library][0]:
            top[variant.library] = (found, name)
    return {name for _, name in top.values()}


def cells(selection: list[str] | None = None, *, newest_only: bool = False) -> list[tuple[str, str]]:
    """(python, variant) of the cells a selection names, in the matrix's order; all of them for an empty one. The weekly
    run (newest_only) takes each library's newest series only."""
    tokens = selection or list(LIBRARIES)
    pythons: dict[str, set[str]] = {}
    for token in tokens:
        library, _, python = token.partition("@")
        pythons.setdefault(library, set()).update([python] if python else PYTHONS)
    wanted = newest() if newest_only else set(VARIANTS)
    return [
        (python, name)
        for name, variant in VARIANTS.items()
        for python in PYTHONS
        if name in wanted and python in pythons.get(variant.library, ())
    ]


def fixtures(selection: list[str], *, newest_only: bool = False) -> list[tuple[str, str]]:
    """(fixture, python) of the images the selected cells run in; fixture `app` is the plain fixture-app."""
    found = {(VARIANTS[name].fixture or "app", python) for python, name in cells(selection, newest_only=newest_only)}
    return sorted(found, key=lambda item: (item[0], number(item[1])))


def upgrades(fixture: str) -> list[str]:
    """What `newest` upgrades in a fixture: the libraries whose newest series' cells run in it (botocore through
    boto3)."""
    libraries = {VARIANTS[name].library for name in newest() if (VARIANTS[name].fixture or "app") == fixture}
    return sorted(NEWEST_THROUGH.get(library, library) for library in libraries)


# --- the weekly run on the newest releases ---------------------------------------------------------------------------

Verdict = namedtuple("Verdict", ["passed", "text", "needs_cell"], defaults=(False,))


def proves(cell: dict) -> bool:
    """A passing cell: the workload started and served, the calls arrived, and an agent framework's steps too."""
    return cell.get("started") is True and cell.get("traced") is True and cell.get("steps") is not False


def segments(passing: set[str], failing: set[str]) -> list[list[str]]:
    """The proven range of a library's series on one Python: from its oldest passing cell to its newest, broken where a
    cell between them fails. The same rule as the package's (impact_instrumentation.qualification.segments)."""
    runs: list[list[str]] = []
    broken = True
    for value in sorted(passing | (failing - passing), key=number):
        if value not in passing:
            broken = True
        elif broken:
            runs.append([value])
            broken = False
        else:
            runs[-1].append(value)
    return runs


def inside(value: str | None, runs: list[list[str]]) -> bool:
    return value is not None and any(number(run[0]) <= number(value) <= number(run[-1]) for run in runs)


def newest_verdict(table: dict, result: dict) -> Verdict:
    """Whether a cell measured on its library's newest release proves the library as the committed cells of that
    library on that Python do. A release that fails is excluded in the next kit release (the run opens an issue). One
    that passes in a series outside the proven range needs a cell before it is traced: the range widens only by a cell,
    so the run fails and its issue says to add it (`make qualify-python`); until then, workloads on it start without
    the kit for it."""
    label = f"{result['library']} {result['version']} on Python {result['python']}"
    if not proves(result):
        answers = ", ".join(f"{field} {result[field]}" for field in ("started", "traced", "content", "steps"))
        return Verdict(False, f"{label}: fails its cell ({answers}); exclude it in the next kit release")
    here = [
        cell
        for cell in table.get("cells", [])
        if cell.get("library") == result["library"] and cell.get("python") == result["python"]
    ]
    committed = [cell for cell in here if proves(cell)]
    contents = {cell.get("content") for cell in committed}
    if committed and result["content"] not in contents:
        return Verdict(
            False,
            f"{label}: its content arrived {result['content']}, the qualification says {sorted(map(str, contents))}",
        )
    runs = segments({str(c.get("major")) for c in committed}, {str(c.get("major")) for c in here})
    if not inside(series(result["version"]), runs):
        return Verdict(
            False,
            f"{label}: passes, in a series no cell proves: it is not traced until `make qualify-python` adds its cell",
            True,
        )
    return Verdict(True, f"{label}: passes, inside the proven range")


def newest_report(directory: Path, table: dict, expected: int | None = None) -> tuple[bool, str]:
    """(every release is traced as it is, a Markdown summary) of the weekly run's cells written to `directory`.
    `expected` cells (each library's newest series on every Python, by default) must have written their answers, else a
    cell that broke before it could is missed."""
    expected = len(cells(newest_only=True)) if expected is None else expected
    results = [json.loads(path.read_text()) for path in sorted(Path(directory).glob("*.json"))]
    verdicts = [newest_verdict(table, result) for result in sorted(results, key=key_of_result)]

    def mark(verdict: Verdict) -> str:
        return "passes" if verdict.passed else "**needs a cell**" if verdict.needs_cell else "**fails**"

    lines = [f"- {mark(v)}: {v.text}" for v in verdicts]
    if len(results) < expected:
        lines.append(f"- **fails**: {expected - len(results)} of {expected} cells wrote no answer (see the job's log)")
    passed = bool(results) and len(results) >= expected and all(v.passed for v in verdicts)
    if passed:
        head = "Every library's newest release passes its cells."
    elif len(results) >= expected and all(v.passed or v.needs_cell for v in verdicts) and results:
        head = "A newest release passes in a series no cell proves: add its cell, or workloads on it stay untraced."
    else:
        head = "A newest release fails its cell."
    return passed, "\n".join([head, "", *lines]) + "\n"


def key_of_result(result: dict) -> tuple:
    return (result.get("library") or "", number(result.get("python") or "0"))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    if command == "libraries":
        print(" ".join(LIBRARIES))
    elif command == "select":
        print(" ".join(select()))
    elif command == "fixtures" and rest == ["--newest"]:
        for kind, python in fixtures([], newest_only=True):
            print(kind, python)
    elif command == "fixtures":
        for kind, python in fixtures(parse(" ".join(rest)) if rest else []):
            print(kind, python)
    elif command == "report" and len(rest) == 1:
        passed, summary = newest_report(Path(rest[0]), json.loads(TABLE.read_text()))
        print(summary, end="")
        return 0 if passed else 1
    elif command == "upgrades" and len(rest) == 1:
        print(" ".join(upgrades(rest[0])))
    elif command == "assemble" and len(rest) == 2:
        out = Path(rest[1])
        committed = json.loads(out.read_text()) if out.is_file() else None
        table = assemble(Path(rest[0]), committed)
        write(table, out)
        passing = sum(1 for cell in table["cells"] if proves(cell))
        print(f"{out}: {len(table['cells'])} cells, {passing} passing", file=sys.stderr)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

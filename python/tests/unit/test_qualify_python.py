# SPDX-License-Identifier: Apache-2.0
"""scripts/qualify_python.py: the Python image's qualification matrix, python/qualification.json in the form the script
writes, and the cells a change touches (what a pull request runs)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_python as qualify  # noqa: E402

TABLE = ROOT / "python" / "qualification.json"
EVERY = set(qualify.LIBRARIES)


def cell(python: str, library: str, major: str, **answers) -> dict:
    return {
        "python": python,
        "library": library,
        "major": major,
        "version": f"{major}.0.0",
        "setting": qualify.GENAI,
        "started": answers.get("started", True),
        "traced": answers.get("traced", True),
        "content": answers.get("content", "span"),
        "steps": answers.get("steps"),
    }


def write(directory: Path, cells: list[dict]) -> Path:
    directory.mkdir(exist_ok=True)
    for index, item in enumerate(cells):
        (directory / f"cell-{index}.json").write_text(json.dumps(item))
    return directory


# --- the committed table -------------------------------------------------------------------------------------------


def committed() -> dict:
    return json.loads(TABLE.read_text())


def test_the_committed_table_is_in_the_form_the_script_writes():
    table = committed()
    assert table["schema"] == "impact.python.qualification.v1"
    assert TABLE.read_text() == json.dumps(table, indent=2) + "\n"
    assert all(tuple(c) == qualify.FIELDS for c in table["cells"])
    assert table["cells"] == sorted(table["cells"], key=qualify.key), "sorted, so that a change shows as a small diff"
    qualify.check(table)


def test_the_committed_table_holds_every_cell_of_the_matrix_and_nothing_else():
    expected = {
        (python, variant.library, qualify.series(qualify.pinned(variant.fixture, variant.library)))
        for variant in qualify.VARIANTS.values()
        for python in qualify.PYTHONS
    }
    assert {(c["python"], c["library"], c["major"]) for c in committed()["cells"]} == expected


def test_each_cell_was_measured_at_the_version_its_fixture_pins_with_the_setting_of_its_library():
    variants = {(v.library, qualify.series(qualify.pinned(v.fixture, v.library))): v for v in qualify.VARIANTS.values()}
    for item in committed()["cells"]:
        variant = variants[(item["library"], item["major"])]
        assert item["version"] == qualify.pinned(variant.fixture, variant.library), item
        assert item["setting"] == variant.setting, item
        assert (item["steps"] is None) == (not variant.agent), "steps are an agent framework's, and only theirs"
        assert item["content"] in ("span", "event", None), item


def test_every_python_the_image_is_built_for_has_its_cells():
    dockerfile = (ROOT / "python" / "Dockerfile").read_text()
    assert [f"3.{n}" for n in (11, 12, 13, 14)] == list(qualify.PYTHONS)
    for python in qualify.PYTHONS:
        assert f"python:{python}-slim@sha256:" in dockerfile


def test_the_libraries_the_image_traces_today_have_a_passing_cell_on_every_python():
    # OpenAI, Anthropic, Bedrock (botocore), the OpenAI Agents SDK, LangChain (and langchain-core) and LangGraph, and
    # Google's SDK, google-genai, which joined the traced set with its cells.
    expected = {
        "openai",
        "anthropic",
        "botocore",
        "openai-agents",
        "langchain",
        "langchain-core",
        "langgraph",
        "google-genai",
    }
    assert set(qualify.LIBRARIES) == expected
    for library in expected:
        passing = {
            c["python"]
            for c in committed()["cells"]
            if c["library"] == library and c["started"] and c["traced"] and c["steps"] is not False
        }
        assert passing == set(qualify.PYTHONS), library


# --- assembling the measured cells ---------------------------------------------------------------------------------


def test_the_cells_are_written_sorted_with_the_schema(tmp_path):
    directory = write(
        tmp_path / "cells",
        [
            cell("3.14", "openai", "3"),
            cell("3.11", "openai", "3"),
            cell("3.11", "anthropic", "1"),
            cell("3.11", "x", "10"),
        ],
    )
    out = tmp_path / "qualification.json"
    assert qualify.main(["assemble", str(directory), str(out)]) == 0
    table = json.loads(out.read_text())
    assert table["schema"] == qualify.SCHEMA
    assert [(c["python"], c["library"], c["major"]) for c in table["cells"]] == [
        ("3.11", "anthropic", "1"),
        ("3.11", "openai", "3"),
        ("3.11", "x", "10"),
        ("3.14", "openai", "3"),
    ]
    assert table["excluded"] == []
    assert out.read_text().endswith("}\n")


def test_a_python_and_a_major_sort_by_number_not_by_text():
    cells = [cell("3.9", "a", "10"), cell("3.11", "a", "9"), cell("3.11", "a", "0.22"), cell("3.11", "a", "0.3")]
    ordered = sorted(cells, key=qualify.key)
    assert [(c["python"], c["major"]) for c in ordered] == [
        ("3.9", "10"),
        ("3.11", "0.3"),
        ("3.11", "0.22"),
        ("3.11", "9"),
    ]


def test_the_excluded_releases_of_the_committed_table_are_kept(tmp_path):
    directory = write(tmp_path / "cells", [cell("3.12", "openai", "3")])
    out = tmp_path / "qualification.json"
    excluded = [{"library": "openai", "version": "3.43.0", "why": "a release that broke its cell"}]
    out.write_text(json.dumps({"schema": qualify.SCHEMA, "cells": [], "excluded": excluded}))
    assert qualify.main(["assemble", str(directory), str(out)]) == 0
    assert json.loads(out.read_text())["excluded"] == excluded


def test_no_cells_is_an_error_that_says_what_to_run(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="make qualify-python"):
        qualify.assemble(tmp_path / "empty")


def test_two_cells_for_one_place_in_the_matrix_are_refused(tmp_path):
    directory = write(tmp_path / "cells", [cell("3.12", "openai", "3"), cell("3.12", "openai", "3", traced=False)])
    with pytest.raises(SystemExit, match="two cells"):
        qualify.assemble(directory)


def test_a_cell_with_other_fields_than_the_schema_is_refused(tmp_path):
    odd = cell("3.12", "openai", "3")
    del odd["steps"]
    with pytest.raises(SystemExit, match="fields"):
        qualify.assemble(write(tmp_path / "cells", [odd]))


# --- what a change touches -----------------------------------------------------------------------------------------


def text(*cells: dict, excluded=()) -> str:
    return json.dumps({"schema": qualify.SCHEMA, "cells": list(cells), "excluded": list(excluded)})


def test_a_new_qualification_touches_every_library_it_names():
    after = text(cell("3.12", "openai", "3"), cell("3.12", "langgraph", "1"))
    assert qualify.touched("python/qualification.json", None, after) == {"openai", "langgraph"}


def test_a_cell_that_changes_is_added_or_goes_touches_its_library_only():
    before = text(cell("3.12", "openai", "3"), cell("3.12", "anthropic", "1"), cell("3.12", "botocore", "1"))
    after = text(
        cell("3.12", "openai", "3", traced=False), cell("3.12", "anthropic", "1"), cell("3.13", "langgraph", "1")
    )
    assert qualify.touched("python/qualification.json", before, after) == {"openai", "botocore", "langgraph"}


def test_an_excluded_release_touches_its_library():
    before = text(cell("3.12", "openai", "3"))
    after = text(cell("3.12", "openai", "3"), excluded=[{"library": "openai", "version": "3.43.0", "why": "x"}])
    assert qualify.touched("python/qualification.json", before, after) == {"openai"}


def test_a_qualification_that_cannot_be_read_touches_every_library():
    assert qualify.touched("python/qualification.json", "{", text()) == EVERY


def lock(**pins: str) -> str:
    return "".join(f"{name}=={version} \\\n    --hash=sha256:{'a' * 64}\n" for name, version in pins.items())


def test_a_new_instrumentation_in_the_lock_touches_its_libraries():
    before = lock(**{"opentelemetry-sdk": "1.45.2"})
    after = lock(**{"opentelemetry-sdk": "1.45.2", "opentelemetry-instrumentation-genai-anthropic": "1.4b0"})
    assert qualify.touched("python/requirements.lock", before, after) == {"anthropic"}
    google = lock(**{"opentelemetry-instrumentation-google-genai": "1.4b0"})
    assert qualify.touched("python/requirements.lock", lock(), google) == {"google-genai"}
    langchain = lock(**{"opentelemetry-instrumentation-genai-langchain": "1.4b0"})
    assert qualify.touched("python/requirements.lock", lock(), langchain) == {
        "langchain",
        "langchain-core",
        "langgraph",
    }


def test_any_other_package_of_the_payload_touches_every_library():
    before = lock(**{"opentelemetry-util-genai": "1.3b0", "wrapt": "2.5.2"})
    after = lock(**{"opentelemetry-util-genai": "1.4b0", "wrapt": "2.5.2"})
    assert qualify.touched("python/requirements.lock", before, after) == EVERY
    assert qualify.touched("python/requirements.lock", before, before) == set()


@pytest.mark.parametrize(
    "path",
    [
        "python/src/impact_instrumentation/runtime.py",
        "python/src/sitecustomize.py",
        "python/Dockerfile",
        "python/tools/assemble_payload.py",
        "python/tests/image/test_python_cells.py",
        "python/tests/image/conftest.py",
        "testing/mock/server.py",
        "testing/apps/Dockerfile",
        "scripts/qualify_python.py",
        "scripts/python-cells.sh",
    ],
)
def test_a_file_every_cell_runs_on_touches_every_library(path):
    assert qualify.touched(path, "a", "b") == EVERY
    # on the canary Python only, so that a change to the package keeps the end-to-end job short
    assert qualify.scope(path, "a", "b") == (set(), EVERY)


def test_a_library_s_own_inputs_run_on_every_python_and_the_payload_s_other_packages_on_the_canary():
    before = lock(**{"opentelemetry-util-genai": "1.3b0", "opentelemetry-instrumentation-genai-openai": "1.3b0"})
    after = lock(**{"opentelemetry-util-genai": "1.4b0", "opentelemetry-instrumentation-genai-openai": "1.4b0"})
    assert qualify.scope("python/requirements.lock", before, after) == ({"openai"}, EVERY)
    assert qualify.scope("testing/apps/requirements-agents.txt", "a", "b")[1] == set()


@pytest.mark.parametrize(
    ("path", "libraries"),
    [
        ("testing/apps/requirements.txt", {"openai", "anthropic", "botocore"}),
        ("testing/apps/app_asgi.py", {"openai", "anthropic", "botocore"}),
        ("testing/apps/requirements-agents.txt", {"openai-agents", "langchain", "langchain-core", "langgraph"}),
        ("testing/apps/app_agents.py", {"openai-agents", "langchain", "langchain-core", "langgraph"}),
        ("testing/apps/requirements-google-genai.txt", {"google-genai"}),
        ("testing/apps/requirements-google-genai-oldest.txt", {"google-genai"}),
        ("testing/apps/app_google_genai.py", {"google-genai"}),
    ],
)
def test_a_fixture_touches_the_libraries_whose_cells_run_in_it(path, libraries):
    assert qualify.touched(path, "a", "b") == libraries


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "chart/impact-ai/values.yaml",
        "nodejs/src/impact/plan.js",
        "nodejs/qualification.json",
        "reporter/main.go",
        "testing/apps/app_wsgi.py",
        "testing/apps/requirements-langgraph.txt",
        "testing/apps/requirements-agents.in",
        "testing/apps/requirements-google-genai.in",
        "python/requirements.in",
        "python/tests/unit/test_runtime.py",
        "python/tests/image/test_agents.py",
    ],
)
def test_a_change_elsewhere_touches_no_cell(path):
    assert qualify.touched(path, "a", "b") == set()


@pytest.mark.parametrize(
    ("value", "libraries"),
    [
        ("all", list(qualify.LIBRARIES)),
        ("none", []),
        ("openai", ["openai"]),
        ("langgraph, openai", ["langgraph", "openai"]),
    ],
)
def test_cells_names_the_libraries_in_place_of_the_change(value, libraries):
    assert qualify.select({"CELLS": value}) == libraries


def test_cells_refuses_a_library_the_matrix_does_not_have():
    with pytest.raises(SystemExit, match="does not have"):
        qualify.select({"CELLS": "crewai"})
    with pytest.raises(SystemExit, match="Pythons the matrix does not have"):
        qualify.select({"CELLS": "openai@3.9"})


def test_cells_can_name_a_library_on_one_python():
    assert qualify.select({"CELLS": "openai@3.13, langgraph"}) == ["langgraph", "openai@3.13"]
    assert qualify.cells(["openai@3.13", "langgraph"]) == [
        ("3.13", "openai-3"),
        *[(python, "langgraph-1") for python in qualify.PYTHONS],
        ("3.13", "openai-1"),
        *[(python, "langgraph-0.2") for python in qualify.PYTHONS],
    ]


def test_each_library_has_a_cell_at_its_newest_series_and_one_at_its_oldest():
    series = {}
    for variant in qualify.VARIANTS.values():
        series.setdefault(variant.library, []).append(qualify.series(qualify.pinned(variant.fixture, variant.library)))
    assert {library: sorted(found, key=qualify.number) for library, found in series.items()} == {
        "openai": ["1", "3"],
        "anthropic": ["0.51", "2"],
        "botocore": ["1"],
        "openai-agents": ["0.3", "0.24"],
        "langchain": ["0.3", "1"],
        "langchain-core": ["0.3", "1"],
        "langgraph": ["0.2", "1"],
        "google-genai": ["1", "2"],
    }


def test_the_weekly_run_takes_each_library_s_newest_series_only():
    assert qualify.newest() == {
        "openai-3",
        "anthropic-2",
        "botocore-1",
        "openai-agents-0.24",
        "langchain-1",
        "langchain-core-1",
        "langgraph-1",
        "google-genai-2",
    }
    assert len(qualify.cells(newest_only=True)) == len(qualify.LIBRARIES) * len(qualify.PYTHONS)
    assert {kind for kind, _ in qualify.fixtures([], newest_only=True)} == {"app", "agents", "google-genai"}


def test_no_selection_is_every_cell_of_the_matrix():
    assert len(qualify.cells([])) == len(qualify.VARIANTS) * len(qualify.PYTHONS)


def git(repository: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    """A repository with the files the rules read, its main branch and a pull request's merge commit."""
    root = tmp_path / "repository"
    (root / "python").mkdir(parents=True)
    (root / "testing" / "apps").mkdir(parents=True)
    (root / "python" / "qualification.json").write_text(text(cell("3.12", "openai", "3")) + "\n")
    (root / "README.md").write_text("kit\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "-m", "base")
    monkeypatch.setattr(qualify, "ROOT", root)
    return root


def test_a_change_is_read_against_the_merge_base_with_main(repository):
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "checkout", "-q", "-b", "change")
    (repository / "testing" / "apps" / "requirements-agents.txt").write_text("langgraph==1.2.19\n")
    (repository / "README.md").write_text("kit, changed\n")
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "--no-verify", "-m", "change")
    agents = ["langchain", "langchain-core", "langgraph", "openai-agents"]
    assert qualify.select({}) == agents
    (repository / "testing" / "apps" / "requirements.txt").write_text("openai==4.0.0\n")  # not committed yet: counts
    assert qualify.select({}) == sorted([*agents, "anthropic", "botocore", "openai"])


def test_in_ci_the_change_is_read_against_the_first_parent_of_the_merge_commit(repository):
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "-q", "-b", "pull")
    path = repository / "python" / "qualification.json"
    path.write_text(text(cell("3.12", "openai", "3"), cell("3.12", "langgraph", "1")) + "\n")
    git(repository, "commit", "-q", "--no-verify", "-am", "qualify langgraph")
    git(repository, "checkout", "-q", "main")
    (repository / "README.md").write_text("main moved on\n")
    git(repository, "commit", "-q", "--no-verify", "-am", "main")
    git(repository, "merge", "-q", "--no-ff", "--no-edit", "pull")  # what CI checks out for a pull request
    assert qualify.select({"GITHUB_ACTIONS": "true"}) == ["langgraph"]
    assert qualify.select({"CELLS_BASE": base}) == ["langgraph"]


def test_a_change_to_the_package_runs_every_library_on_the_canary_python_and_a_fixture_s_on_every_python(repository):
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "checkout", "-q", "-b", "change")
    (repository / "python" / "src").mkdir()
    (repository / "python" / "src" / "runtime.py").write_text("changed\n")
    assert qualify.select({}) == sorted(f"{library}@{qualify.CANARY_PYTHON}" for library in qualify.LIBRARIES)
    (repository / "testing" / "apps" / "requirements.txt").write_text("openai==4.0.0\n")
    canary = sorted(f"{library}@{qualify.CANARY_PYTHON}" for library in qualify.LIBRARIES if library not in FIXTURE_APP)
    assert qualify.select({}) == sorted(FIXTURE_APP) + canary


FIXTURE_APP = {"openai", "anthropic", "botocore"}


def test_when_the_change_cannot_be_read_every_cell_runs(repository, capsys):
    assert qualify.select({"CELLS_BASE": "no-such-revision"}) == list(qualify.LIBRARIES)
    assert "every cell" in capsys.readouterr().err


# --- the images the cells run in -----------------------------------------------------------------------------------


def test_the_fixtures_of_the_cells_are_one_image_per_python_and_kind():
    assert qualify.fixtures(["langgraph"]) == [
        *[("agents", python) for python in qualify.PYTHONS],
        *[("oldest", python) for python in qualify.PYTHONS],
    ]
    assert qualify.fixtures(["botocore"]) == [("app", python) for python in qualify.PYTHONS]
    kinds = {kind for kind, _ in qualify.fixtures([])}
    assert kinds == {"app", "agents", "oldest", "google-genai", "google-genai-oldest"}
    assert qualify.fixtures(["google-genai@3.13"]) == [("google-genai", "3.13"), ("google-genai-oldest", "3.13")]


def test_the_fixtures_follow_the_pythons_of_the_selection():
    assert qualify.fixtures(["openai@3.12", "langgraph@3.12"]) == [
        ("agents", "3.12"),
        ("app", "3.12"),
        ("oldest", "3.12"),
    ]


def test_the_weekly_run_upgrades_each_fixture_s_libraries_and_botocore_through_boto3():
    assert qualify.upgrades("app") == ["anthropic", "boto3", "openai"]
    assert qualify.upgrades("agents") == ["langchain", "langchain-core", "langgraph", "openai-agents"]
    assert qualify.upgrades("oldest") == [], "the oldest fixture keeps its pins"


def test_every_fixture_names_a_requirements_file_and_an_app_that_exist():
    for variant in qualify.VARIANTS.values():
        assert (qualify.APPS / qualify.requirements(variant.fixture)).is_file(), variant
        assert (qualify.APPS / f"{variant.app}.py").is_file(), variant
        assert qualify.pinned(variant.fixture, variant.library), variant


# --- the weekly run on the newest releases -------------------------------------------------------------------------


def measured(python: str, library: str, version: str, **answers) -> dict:
    result = cell(python, library, qualify.series(version), **answers)
    result["version"] = version
    return result


COMMITTED = {
    "schema": qualify.SCHEMA,
    "cells": [
        cell("3.12", "openai", "1"),
        cell("3.12", "openai", "3"),
        cell("3.12", "langgraph", "1", steps=True),
        cell("3.12", "openai-agents", "0.3", steps=True),
        cell("3.12", "openai-agents", "0.24", steps=True),
    ],
}


def test_a_newest_release_inside_the_proven_range_passes():
    verdict = qualify.newest_verdict(COMMITTED, measured("3.12", "openai", "3.50.0"))
    assert verdict == (True, "openai 3.50.0 on Python 3.12: passes, inside the proven range", False)


@pytest.mark.parametrize(("library", "version"), [("openai", "4.0.1"), ("openai-agents", "0.25.0")])
def test_a_newest_release_of_a_series_no_cell_proves_fails_the_run_and_asks_for_its_cell(library, version):
    # A new major, or a new minor of a library that versions by minor: workloads on it are not traced until its cell
    # is added, so the weekly run must say so rather than pass.
    verdict = qualify.newest_verdict(COMMITTED, measured("3.12", library, version, steps=True))
    assert not verdict.passed and verdict.needs_cell
    assert verdict.text == (
        f"{library} {version} on Python 3.12: passes, in a series no cell proves: it is not traced until "
        "`make qualify-python` adds its cell"
    )


def test_the_report_of_a_release_that_needs_a_cell_fails_and_says_so(tmp_path):
    results = write(tmp_path / "new", [measured("3.12", "openai", "4.0.1"), measured("3.12", "langgraph", "1.3.0")])
    passed, summary = qualify.newest_report(results, COMMITTED, expected=2)
    assert not passed
    assert summary.startswith("A newest release passes in a series no cell proves: add its cell")
    assert "- **needs a cell**: openai 4.0.1 on Python 3.12" in summary
    assert "- passes: langgraph 1.3.0 on Python 3.12" in summary
    assert qualify.main(["report", str(results)]) == 1


@pytest.mark.parametrize(
    "answers", [{"started": False}, {"traced": False}, {"steps": False}], ids=["started", "traced", "steps"]
)
def test_a_newest_release_that_fails_its_cell_fails_the_run_and_says_to_exclude_it(answers):
    library = "langgraph" if "steps" in answers else "openai"
    version = "1.9.0" if library == "langgraph" else "3.50.0"
    verdict = qualify.newest_verdict(COMMITTED, measured("3.12", library, version, **answers))
    assert not verdict.passed
    assert verdict.text.startswith(f"{library} {version} on Python 3.12: fails its cell")
    assert "exclude it in the next kit release" in verdict.text


def test_a_newest_release_whose_content_moved_fails_the_run():
    verdict = qualify.newest_verdict(COMMITTED, measured("3.12", "openai", "3.50.0", content="event"))
    assert not verdict.passed and "content arrived event" in verdict.text


def test_the_report_fails_when_a_cell_fails_or_wrote_no_answer(tmp_path):
    good = write(tmp_path / "good", [measured("3.12", "openai", "3.50.0")])
    assert qualify.newest_report(good, COMMITTED, expected=1) == (
        True,
        "Every library's newest release passes its cells.\n\n"
        "- passes: openai 3.50.0 on Python 3.12: passes, inside the proven range\n",
    )
    passed, summary = qualify.newest_report(good, COMMITTED, expected=2)
    assert not passed and "1 of 2 cells wrote no answer" in summary
    bad = write(tmp_path / "bad", [measured("3.12", "openai", "3.50.0", traced=False)])
    passed, summary = qualify.newest_report(bad, COMMITTED, expected=1)
    assert not passed and "- **fails**: openai 3.50.0" in summary
    (tmp_path / "none").mkdir()
    assert not qualify.newest_report(tmp_path / "none", COMMITTED, expected=0)[0]


def test_google_genai_has_a_cell_at_its_instrumentation_s_floor_and_one_at_its_newest_series():
    # 1.32.0 is the floor of opentelemetry-instrumentation-google-genai (google-genai>=1.32.0,<3): the range runs from
    # the 1.x cell to the 2.x one, and the weekly run upgrades only the newest series' fixture.
    pinned = {
        name: qualify.pinned(variant.fixture, variant.library)
        for name, variant in qualify.VARIANTS.items()
        if variant.library == "google-genai"
    }
    assert pinned == {"google-genai-1": "1.32.0", "google-genai-2": "2.34.0"}
    assert "google-genai-2" in qualify.newest()
    assert qualify.upgrades("google-genai") == ["google-genai"]
    assert qualify.upgrades("google-genai-oldest") == []


def test_google_genai_cells_call_both_apis_sync_async_and_streaming():
    for name in ("google-genai-1", "google-genai-2"):
        variant = qualify.VARIANTS[name]
        assert variant.calls == len(variant.requests) == 8
        assert {path.rsplit("/", 2)[1] for path in variant.requests} == {"gemini", "vertex"}
        assert {path.rsplit("/", 1)[1] for path in variant.requests} == {"sync", "async", "stream", "async-stream"}
        assert variant.setting == qualify.GENAI

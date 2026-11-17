# SPDX-License-Identifier: Apache-2.0

"""CI runs the end-to-end suite once a day and at a release, each job under ten minutes (scripts/ci_jobs.py).

A pull request, a draft too, and a push to main run `make ci` in its parts. The end-to-end suite, on Kubernetes 1.34
and 1.27, and the overhead measurements run in the daily run on main, when main changed in a way they can see; the
suite runs at a release, on the tagged commit before anything is published; and a pull request asks for them with a
label.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_jobs  # noqa: E402


def tracked() -> list[str]:
    out = subprocess.run(  # noqa: S603 - fixed command
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [name for name in out.split("\0") if name]


def outputs_of(tmp_path, argv, environ=None) -> dict[str, str]:
    out = tmp_path / "output"
    assert ci_jobs.main(argv, {**(environ or {}), "GITHUB_OUTPUT": str(out)}) == 0
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


@pytest.mark.parametrize("paths", [["python/src/impact_instrumentation/detect.py", "chart/impact-ai/values.yaml"], []])
def test_a_pull_request_and_a_push_to_main_run_the_test_parts_only_whatever_they_touch(paths):
    for event in ("pull_request", "push"):
        jobs = ci_jobs.select(event, [], paths)
        assert jobs["test"] == ["checks", "python", "nodejs"], event
        assert (jobs["e2e"], jobs["e2e-oldest"], jobs["overhead"]) == (False, False, False), event


def test_a_release_runs_the_suite_on_both_versions_and_leaves_cells_and_overhead_to_make_release(tmp_path):
    jobs = ci_jobs.select("release", [], [])
    assert (jobs["test"], jobs["e2e"], jobs["e2e-oldest"], jobs["overhead"]) == ([], True, True, False)
    # the release workflow calls ci.yml with CI_JOBS_RELEASE=true, for the push of its tag
    called = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push", "CI_JOBS_RELEASE": "true"}
    outputs = outputs_of(tmp_path, [], called)
    assert (outputs["test"], outputs["images"], outputs["cells"], outputs["overhead"]) == ("[]", "true", "", "false")
    assert json.loads(outputs["kind"]) == json.loads(outputs["oldest"]) == ci_jobs.kind_shards()
    assert json.loads(outputs["containers"]) == ["python", "nodejs"]


@pytest.mark.parametrize(
    "path",
    [
        "chart/impact-ai/README.md",  # e2e/test_5_uninstall.py reads it
        "chart/impact-ai/values.yaml",
        "e2e/test_1_install.py",
        "python/src/impact_instrumentation/detect.py",
        "python/uv.lock",
        "python/requirements.lock",
        "python/qualification.json",
        "nodejs/trees/node22/package-lock.json",
        "nodejs/tests/image/test_nodejs_matrix.py",
        "reporter/go.mod",
        "testing/apps/requirements-agents.txt",
        "testing/apps/nodejs/Dockerfile",
        "testing/mock/server.py",
        "scripts/e2e.sh",
        "scripts/uninstall-cluster-kit.sh",
        "scripts/ci_jobs.py",
        "Makefile",
        "mise.toml",
        ".github/workflows/ci.yml",
        "a/directory/that/is/new.txt",
    ],
)
def test_the_daily_run_takes_a_day_that_changed_what_the_end_to_end_job_sees(path):
    jobs = ci_jobs.select("schedule", [], ["README.md", path], "example/kit")
    assert (jobs["test"], jobs["e2e"], jobs["e2e-oldest"], jobs["overhead"]) == ([], True, True, True)
    assert path in jobs["why"]["e2e"]


@pytest.mark.parametrize(
    "paths",
    [
        ["README.md", "SUPPORT.md", "SECURITY.md", "AGENTS.md"],
        ["CHANGELOG.md", "docs/install.md", ".github/ISSUE_TEMPLATE/bug.yml", ".github/PULL_REQUEST_TEMPLATE.md"],
        ["LICENSE", "NOTICE", ".trivyignore", ".github/workflows/release.yml"],
        ["java/Dockerfile", "testing/apps/java/Dockerfile"],
        ["chart/tests/test_chart.py", "python/tests/unit/test_lock.py", "nodejs/tests/package-lock.json"],
        ["scripts/release.sh", "scripts/verify-release.sh", "scripts/overhead.sh"],
        ["testing/apps/datadog/cells.py", "testing/apps/newrelic_app.py", "testing/fixtures/sources/x.json"],
        [],
    ],
)
def test_the_daily_run_skips_a_day_that_changed_nothing_the_end_to_end_job_sees(paths):
    jobs = ci_jobs.select("schedule", [], paths, "example/kit")
    assert (jobs["test"], jobs["e2e"], jobs["e2e-oldest"], jobs["overhead"]) == ([], False, False, False)


def test_the_daily_run_runs_when_the_days_change_cannot_be_read_and_never_where_releases_are_made():
    assert ci_jobs.select("schedule", [], None, "example/kit")["e2e"] is True
    released = ci_jobs.select("schedule", [], ["python/src/x.py"], ci_jobs.release_repository().upper())
    assert (released["e2e"], released["e2e-oldest"], released["overhead"]) == (False, False, False)


def test_the_daily_run_takes_what_changed_since_the_newest_commit_older_than_a_day_and_two_hours():
    head = ci_jobs.git("rev-parse", "HEAD").strip()
    committed = int(ci_jobs.git("log", "-1", "--format=%ct", "HEAD"))
    assert ci_jobs.window_base(now=committed + 27 * 3600) == head, "nothing changed since: an empty change"
    assert ci_jobs.window_base(now=committed + 1 * 3600) != head, "the head changed within the window"


def test_the_labels_ask_for_the_long_jobs():
    full = ci_jobs.select("pull_request", ["run-full-ci"], ["README.md"])
    assert (full["e2e"], full["e2e-oldest"], full["overhead"]) == (True, True, False)
    benchmark = ci_jobs.select("pull_request", ["benchmark"], ["README.md"])
    assert (benchmark["e2e"], benchmark["e2e-oldest"], benchmark["overhead"]) == (False, False, True)
    other = ci_jobs.select("pull_request", ["dependencies"], ["README.md"])
    assert (other["e2e"], other["e2e-oldest"], other["overhead"]) == (False, False, False)


def test_every_file_left_out_is_still_there():
    files = tracked()
    for pattern, reason in ci_jobs.NOT_E2E:
        assert reason.strip()
        assert any(ci_jobs.matches(pattern, path) for path in files), f"{pattern} matches no file: remove it"


def test_nothing_the_end_to_end_job_builds_deploys_runs_or_reads_is_left_out():
    seen = (
        "e2e/",
        "chart/impact-ai/",
        "python/src/",
        "python/tools/",
        "python/tests/image/",
        "nodejs/src/",
        "nodejs/shims/",
        "nodejs/trees/",
        "nodejs/tools/",
        "nodejs/tests/image/",
        "reporter/",
        "testing/mock/",
        "testing/apps/nodejs/",
    )
    read = {
        "Makefile",
        "mise.toml",
        ".github/workflows/ci.yml",
        "python/Dockerfile",
        "python/pyproject.toml",
        "python/uv.lock",
        "python/requirements.lock",
        "python/qualification.json",
        "nodejs/Dockerfile",
        "nodejs/qualification.json",
        "testing/apps/Dockerfile",
        "testing/apps/requirements.txt",
        "testing/apps/requirements-agents.txt",
        "testing/apps/requirements-langgraph.txt",
        "testing/apps/requirements-oldest.txt",
        "testing/apps/app_asgi.py",
        "testing/apps/app_agents.py",
        "testing/apps/app_ddtrace.py",
        "testing/apps/app_own_otel.py",
        "testing/apps/run_asgi.py",
        "scripts/build-images.sh",
        "scripts/ci_jobs.py",
        "scripts/e2e.sh",
        "scripts/lib.sh",
        "scripts/names.sh",
        "scripts/python-cells.sh",
        "scripts/qualify_python.py",
        "scripts/uninstall-cluster-kit.sh",
    }
    files = tracked()
    assert read <= set(files)
    hidden = sorted(path for path in files if (path in read or path.startswith(seen)) and not ci_jobs.sees(path))
    assert hidden == []


def test_the_kind_shards_run_every_module_of_the_suite_once_in_order():
    shards = ci_jobs.kind_shards()
    named = [module for shard in shards for module in shard["tests"].split()]
    assert sorted(named) == ci_jobs.modules() and len(named) == len(set(named))
    assert all(shard["tests"].split() == sorted(shard["tests"].split()) for shard in shards)
    assert [shard["shard"] for shard in shards] == ["1", "2", "3"]
    # what builds on the cluster another module left stays with it
    together = {
        "e2e/test_8_uninstall.py": "e2e/test_8_auto_cover.py",
        "e2e/test_5_uninstall.py": "e2e/test_1_install.py",
    }
    for module, before in together.items():
        (shard,) = [s["tests"].split() for s in shards if module in s["tests"].split()]
        assert before in shard


def test_a_module_no_shard_names_runs_in_one_more(monkeypatch):
    modules = ci_jobs.modules()
    monkeypatch.setattr(ci_jobs, "modules", lambda: [*modules, "e2e/test_9_new.py"])
    assert ci_jobs.kind_shards()[-1] == {"shard": "4", "tests": "e2e/test_9_new.py"}


def test_the_workflow_reads_matrices_and_flags(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text("python/src/impact_instrumentation/detect.py\n")
    summary = tmp_path / "summary"
    argv = ["--paths", str(changed), "--label", "run-full-ci", "--label", "benchmark"]
    outputs = outputs_of(tmp_path, argv, {"GITHUB_STEP_SUMMARY": str(summary)})
    assert json.loads(outputs["test"]) == ["checks", "python", "nodejs"]
    assert outputs["images"] == "true"
    assert [shard["shard"] for shard in json.loads(outputs["kind"])] == ["1", "2", "3"]
    assert json.loads(outputs["containers"]) == ["python", "nodejs"]
    assert json.loads(outputs["oldest"]) == json.loads(outputs["kind"])
    assert (outputs["cells"], outputs["overhead"]) == ("", "true")
    assert "| overhead | runs | the benchmark label |" in summary.read_text()


def test_a_job_that_does_not_run_gets_an_empty_matrix(tmp_path):
    changed = tmp_path / "changed.txt"
    changed.write_text("README.md\n")
    out = tmp_path / "output"
    assert ci_jobs.main(["--paths", str(changed)], {"GITHUB_OUTPUT": str(out)}) == 0
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert (outputs["images"], outputs["kind"], outputs["containers"], outputs["oldest"]) == ("false", "[]", "[]", "[]")


def test_a_draft_runs_the_test_parts_only_whatever_its_labels(tmp_path):
    labels = ["run-full-ci", "benchmark"]
    jobs = ci_jobs.select("pull_request", labels, ["python/src/impact_instrumentation/detect.py"], draft=True)
    assert jobs["test"] == ["checks", "python", "nodejs"]
    assert (jobs["e2e"], jobs["e2e-oldest"], jobs["overhead"]) == (False, False, False)
    changed = tmp_path / "changed.txt"
    changed.write_text("python/src/impact_instrumentation/detect.py\n")
    out = tmp_path / "output"
    argv = ["--paths", str(changed), "--draft", "--label", "run-full-ci"]
    assert ci_jobs.main(argv, {"GITHUB_OUTPUT": str(out)}) == 0
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert json.loads(outputs["test"]) == ["checks", "python", "nodejs"]
    assert (outputs["images"], outputs["kind"], outputs["oldest"], outputs["overhead"]) == (
        "false",
        "[]",
        "[]",
        "false",
    )

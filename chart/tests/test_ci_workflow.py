# SPDX-License-Identifier: Apache-2.0

"""Every job of CI finishes in under ten minutes, runs on what scripts/ci_jobs.py answers, and its actions are pinned.

A job has a time limit of 12 minutes: a job that grows is split into parts, not given longer. Only the overhead
measurements, of the daily run and the benchmark label, take longer. A change runs the parts of `make ci`; the
end-to-end suite runs every day on main, and a release publishes nothing before it passed on the tagged commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_jobs  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
TEXT = (WORKFLOWS / "ci.yml").read_text()
CI = yaml.safe_load(TEXT)
TRIGGERS = CI[True]  # YAML 1.1 reads the key `on` as true
JOBS = CI["jobs"]
RELEASE = yaml.safe_load((WORKFLOWS / "release.yml").read_text())


def needs_of(job: dict) -> list[str]:
    return [job["needs"]] if isinstance(job.get("needs"), str) else job.get("needs", [])


def test_a_job_has_twelve_minutes_at_most_and_only_the_overhead_measurements_more():
    for name, job in JOBS.items():
        assert job["timeout-minutes"] <= (25 if name == "overhead" else 12), name


def test_every_job_but_the_first_runs_on_its_answer():
    # The first job runs for every event, a draft too; without its answer, a job would run for a draft, and in the daily
    # run, whatever scripts/ci_jobs.py says.
    assert "if" not in JOBS["changes"]
    for name, job in JOBS.items():
        if name != "changes":
            assert "changes" in needs_of(job), name
            assert "needs.changes.outputs." in job["if"] or "contains(needs.*.result" in job["if"], name


def test_the_workflow_reads_what_the_script_writes():
    written = set(ci_jobs.outputs(ci_jobs.select("push", [], []), ""))
    assert set(JOBS["changes"]["outputs"]) == written
    assert set(re.findall(r"needs\.changes\.outputs\.([a-z-]+)", TEXT)) == written


def test_every_part_the_workflow_runs_is_a_make_target():
    makefile = (ROOT / "Makefile").read_text()
    targets = [f"ci-{part}" for part in ci_jobs.TEST_PARTS] + [f"e2e-{part}" for part in ci_jobs.E2E_PARTS]
    targets += ["ci-jobs", "e2e-images", "e2e-kind", "test-python-cells", "overhead"]
    for target in targets:
        assert re.search(rf"^{target}:", makefile, re.MULTILINE), target


def test_the_two_jobs_a_ruleset_can_require_fail_when_a_part_did_not_pass():
    parts = {"test": {"test-part"}, "e2e": {"e2e-images", "e2e-kind", "e2e-part", "e2e-cells"}}
    for name, of in parts.items():
        job = JOBS[name]
        assert set(needs_of(job)) == {"changes", *of}, name
        assert job["if"] == (
            "${{ !cancelled() && (contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')) }}"
        )
        assert [step["run"] for step in job["steps"]] == ["exit 1"]
    # every job of a part of the end-to-end job is one of them
    assert {n for n in JOBS if n.startswith("e2e-") and n not in ("e2e-oldest", "e2e-newest")} == parts["e2e"]


def test_every_action_is_pinned_by_commit_and_no_checkout_keeps_the_token():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        text = path.read_text()
        for action, comment in re.findall(r"uses: (\S+)(.*)", text):
            if action.startswith("./.github/workflows/"):  # a workflow of this repository, at the same commit
                continue
            # owner/repository, or an action in a directory of it (github/codeql-action/upload-sarif), at a commit
            assert re.fullmatch(r"[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}", action), (path.name, action)
            assert re.fullmatch(r" # v\d+\.\d+\.\d+", comment), (path.name, action, comment)
        for job in yaml.safe_load(text)["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") is False, (path.name, step)


def test_the_daily_run_reads_a_day_of_main_from_its_whole_history():
    assert TRIGGERS["schedule"] == [{"cron": "17 4 * * *"}]
    (checkout, select) = JOBS["changes"]["steps"]
    assert checkout["with"]["fetch-depth"] == "${{ github.event_name == 'schedule' && '0' || '2' }}"
    assert select["run"] == "make ci-jobs"


def test_a_release_publishes_nothing_before_the_suite_passed_on_its_commit():
    assert TRIGGERS["workflow_call"]["inputs"]["release"] == {
        "description": TRIGGERS["workflow_call"]["inputs"]["release"]["description"],
        "type": "boolean",
        "default": False,
    }
    (_, select) = JOBS["changes"]["steps"]
    assert select["env"] == {"CI_JOBS_RELEASE": "${{ inputs.release }}"}
    gate, publish = RELEASE["jobs"]["e2e"], RELEASE["jobs"]["publish"]
    assert (gate["uses"], gate["with"], gate["permissions"]) == (
        "./.github/workflows/ci.yml",
        {"release": True},
        {"contents": "read"},
    )
    # nothing is published before the gate passed, and the GitHub release only after the images and the chart were
    assert publish["needs"] == "e2e"
    assert RELEASE["jobs"]["github-release"]["needs"] == "publish"
    assert list(RELEASE["jobs"]) == ["reviewed", "e2e", "publish", "github-release"]


def test_a_release_is_published_only_from_a_commit_merged_into_main():
    reviewed = RELEASE["jobs"]["reviewed"]
    assert RELEASE["jobs"]["e2e"]["needs"] == "reviewed"
    assert reviewed["if"] == f"github.repository == '{ci_jobs.release_repository()}'"
    checkout, gate = reviewed["steps"]
    assert checkout["with"]["fetch-depth"] == 0, "the history of main, to find the tagged commit on it"
    assert gate["run"] == "make release-reviewed"


def test_outside_contributions_are_checked_for_their_sign_off_in_the_public_repository_only():
    dco = yaml.safe_load((WORKFLOWS / "dco.yml").read_text())
    assert dco["permissions"] == {}
    job = dco["jobs"]["sign-off"]
    # a release's own pull request, a release branch of the repository itself, carries the release identity's commit
    assert job["if"] == (
        f"github.repository == '{ci_jobs.release_repository()}' && !(github.event.pull_request.head.repo.full_name == "
        "github.repository && startsWith(github.head_ref, 'release/'))"
    )
    assert job["permissions"] == {"contents": "read"}
    assert job["steps"][-1]["run"] == "make check-dco"


def test_only_the_github_release_writes_to_the_repository_and_it_runs_the_make_targets():
    jobs = RELEASE["jobs"]
    assert RELEASE["permissions"] == {}
    assert [name for name, job in jobs.items() if job["permissions"].get("contents") == "write"] == ["github-release"]
    assert [step["run"] for step in jobs["github-release"]["steps"] if "run" in step] == [
        "make release-files",
        "make github-release",
    ]
    # it attaches what the publish job pushed and signed, handed over as an artifact, and the provenance of its files
    upload = jobs["publish"]["steps"][-1]
    assert upload["uses"].startswith("actions/upload-artifact@") and upload["with"]["name"] == "release"
    publish = next(step for step in jobs["github-release"]["steps"] if step.get("run") == "make github-release")
    assert publish["env"]["PROVENANCE_BUNDLE"] == "${{ steps.provenance.outputs.bundle-path }}"


# The public repository had this name before it was renamed. The gate of release.yml names it until the owner, who alone
# pushes .github/workflows, changes that line; until then the gate is skipped in the renamed repository, and with it
# everything that needs it, so no release is published there.
FORMER_RELEASE_REPOSITORY = "Impact-AI/impact-ai-k8s"


def test_the_release_gate_runs_only_in_the_repository_releases_are_made_in():
    gate = RELEASE["jobs"]["e2e"]["if"]
    if gate == f"github.repository == '{FORMER_RELEASE_REPOSITORY}'":
        pytest.xfail(
            "release.yml still names the repository by its former name: set its gate to "
            f"github.repository == '{ci_jobs.release_repository()}'"
        )
    # only where make release runs at all (scripts/names.sh)
    assert gate == f"github.repository == '{ci_jobs.release_repository()}'"

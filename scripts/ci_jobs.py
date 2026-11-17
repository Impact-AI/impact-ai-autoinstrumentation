#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""The jobs of CI an event needs: .github/workflows/ci.yml runs this first (`make ci-jobs`), its jobs read the answer.

    python3 scripts/ci_jobs.py                        in a workflow, the event it runs for; elsewhere this change, as a
                                                      pull request: against the merge base with origin/main, or
                                                      CI_JOBS_BASE
    python3 scripts/ci_jobs.py --base REV --head REV  the change from one commit to another
    python3 scripts/ci_jobs.py --paths FILE           the change to the files FILE lists, one a line (- for stdin)
    --event pull_request|push|schedule|release, --label NAME (again for another) and --draft say what it is

What runs, each job under ten minutes:

    test        `make ci`, in parts on runners of their own (TEST_PARTS): every pull request, a draft too, and every
                push to main
    e2e         `make e2e`, in parts: the images once, the kind suite in shards (KIND_SHARDS), the container parts
                (E2E_PARTS) and the cells of the Python qualification the change touches. The daily run on main, a
                release before anything is published (the release workflow calls ci.yml with CI_JOBS_RELEASE=true),
                and a pull request labelled run-full-ci
    e2e-oldest  the kind shards on Kubernetes 1.27: the same
    overhead    `make overhead`: the daily run on main, and a pull request labelled benchmark (a release measures its
                own overhead, every runtime, before it pushes anything)

The daily run takes what changed on main in the last DAILY_HOURS: nothing when main did not change, or changed only in
files the end-to-end job cannot see (NOT_E2E), and nothing in the repository releases are made in, where each release
runs the suite itself. A release runs no cells: `make release` runs every one. A draft runs the parts of `make ci` only,
whatever its labels say. When the change cannot be read, the daily run runs. Standard library only; the cells are
scripts/qualify_python.py's.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ROOT / "scripts" / "names.sh"

FULL_LABEL = "run-full-ci"
BENCHMARK_LABEL = "benchmark"
# The daily run (04:17 UTC) takes what changed on main in this many hours: a day, and two for a run that starts late.
DAILY_HOURS = 26

# `make ci` in parts, each a job of its own: `make ci-checks`, `make ci-python`, `make ci-nodejs`.
TEST_PARTS = ("checks", "python", "nodejs")
# The end-to-end job's container suites, each a job of its own: `make e2e-python`, `make e2e-nodejs`.
E2E_PARTS = ("python", "nodejs")

# The kind suite in shards, each on a cluster of its own: its images loaded from the archives `make e2e-images` leaves
# (about three minutes with the cluster), then its modules in order. A module that builds on the cluster another left
# stays in that one's shard. The minutes are the medians of 14 runs of the end-to-end job, 5 to 10 November 2026.
KIND_SHARDS = (
    # install, cover, logs, the Operator's role, fail open, a new key, one product, uninstall, beside another
    # injector: 5.3 minutes
    (
        "e2e/test_1_install.py",
        "e2e/test_2_cover.py",
        "e2e/test_2_logs.py",
        "e2e/test_2_operator_role.py",
        "e2e/test_3_fail_open.py",
        "e2e/test_4_new_key.py",
        "e2e/test_4_one_product.py",
        "e2e/test_5_uninstall.py",
        "e2e/test_6_report_only.py",
    ),
    # the install token, from a cluster without the kit: 2.0 minutes
    ("e2e/test_7_exchange.py",),
    # automatic coverage, on the token install it makes when it runs without test_7, then its uninstall: 5.5 minutes
    # and the install
    ("e2e/test_8_auto_cover.py", "e2e/test_8_uninstall.py"),
)

# What the end-to-end job cannot see: it does not build, deploy, run or read these files, so a change to them alone
# leaves its result as it was. Every other file runs it, a new one too. A pattern that ends in / is a directory, and *
# stands for any part of one name. The chart's README is not one of them: e2e/test_5_uninstall.py reads it. Each holds a
# file in the public repository too (the publish script leaves Dependabot's configuration and itself out), where
# chart/tests checks that it still does.
NOT_E2E = (
    ("*.md", "the repository's documents"),
    ("docs/", "the documentation the chart README links to"),
    (".github/ISSUE_TEMPLATE/", "GitHub's issue forms"),
    (".github/PULL_REQUEST_TEMPLATE.md", "GitHub's pull request template"),
    ("LICENSE", "the images carry it; the image tests of `make ci` compare it"),
    ("NOTICE", "the images carry it; the image tests of `make ci` compare it"),
    (".gitignore", "git's"),
    (".trivyignore", "the vulnerability scans of `make ci`"),
    (".github/workflows/release.yml", "runs on a tag"),
    ("java/", "the Java image: `make ci` builds and tests it, and the cluster runs no Java workload"),
    ("testing/apps/java/", "the Java image's fixtures, in `make ci`"),
    ("dotnet/", "the .NET proofs: `make ci` builds and runs them, and the cluster runs no .NET workload"),
    ("testing/apps/dotnet/", "the .NET proofs' fixtures, in `make ci`"),
    ("scripts/remove-dotnet-images.sh", "the .NET proofs, in `make ci`"),
    ("chart/tests/", "the chart's render checks and the repository's own tests, in `make ci`"),
    ("python/tests/unit/", "unit tests, in `make ci`"),
    ("python/tests/payload/", "unit tests, in `make ci`"),
    ("python/tests/overhead/", "the overhead measurements: the overhead job, and the canary of `make ci`"),
    ("nodejs/tests/unit/", "the Node.js package's unit tests, in `make ci`"),
    ("nodejs/tests/Dockerfile", "the container of those unit tests"),
    ("nodejs/tests/package.json", "the packages of those unit tests"),
    ("nodejs/tests/package-lock.json", "the packages of those unit tests"),
    ("scripts/check_internal_details.py", "the checks of `make lint` and of a release"),
    ("scripts/check_spdx.py", "a check of `make lint`"),
    ("scripts/ci/", "changes of ci.yml waiting for the code owner"),
    ("scripts/overhead.sh", "the overhead measurements"),
    ("scripts/overhead_report.py", "the overhead measurements"),
    ("scripts/qualify_nodejs.py", "`make qualify`"),
    ("scripts/release.sh", "releases"),
    ("scripts/scan-chart.sh", "a check of `make lint`"),
    ("scripts/scan-images.sh", "the vulnerability scans of `make ci`"),
    ("scripts/stamp_chart.py", "releases"),
    ("scripts/test-nodejs-unit.sh", "the Node.js package's unit tests, in `make ci`"),
    ("scripts/vendor_operator_chart.py", "a check of `make lint`; the chart it vendors is under chart/"),
    ("scripts/verify-release.sh", "releases"),
    ("testing/apps/datadog/", "the Datadog harness (make test-datadog)"),
    ("testing/apps/dynatrace/", "the Dynatrace recordings (make test-dynatrace)"),
    ("testing/apps/newrelic-node/", "the New Relic harness (make test-newrelic)"),
    ("testing/apps/newrelic*", "the New Relic harness (make test-newrelic)"),
    ("testing/apps/otel_tools/", "the recorder of what OpenTelemetry-based tools send"),
    ("testing/fixtures/", "recordings the unit tests and the reporter's tests read, in `make ci`"),
)


def matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    regex = "".join("[^/]*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern))
    return re.fullmatch(regex, path) is not None


def sees(path: str) -> bool:
    """Whether the end-to-end job can see a change to this file."""
    return not any(matches(pattern, path) for pattern, _ in NOT_E2E)


def modules() -> list[str]:
    """The modules of the kind suite."""
    return sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "e2e").glob("test_*.py"))


def kind_shards() -> list[dict[str, str]]:
    """The shards of the kind suite, for the workflow's matrix. A module no shard names runs in one more, last."""
    named = {module for shard in KIND_SHARDS for module in shard}
    rest = tuple(module for module in modules() if module not in named)
    shards = [*KIND_SHARDS, rest] if rest else list(KIND_SHARDS)
    return [{"shard": str(n), "tests": " ".join(shard)} for n, shard in enumerate(shards, 1)]


def release_repository() -> str:
    """The repository releases are made in (scripts/names.sh)."""
    found = re.search(r"^RELEASE_REPOSITORY=(\S+)$", NAMES.read_text(), re.MULTILINE)
    return found.group(1) if found else ""


SUITE = "only in the daily run on main, at a release, or with the run-full-ci label"
ONLY = {
    "e2e": SUITE,
    "e2e-oldest": SUITE,
    "overhead": "only in the daily run on main, or with the benchmark label",
}


def select(
    event: str, labels: list[str], paths: list[str] | None, repository: str = "", *, draft: bool = False
) -> dict:
    """The jobs an event needs, and why. `paths` is the change: of a pull request, or for the daily run what changed on
    main in the last DAILY_HOURS; None when it cannot be read."""
    labels = list(labels)
    if event == "push":
        return {
            "test": list(TEST_PARTS),
            "e2e": False,
            "e2e-oldest": False,
            "overhead": False,
            "why": {"test": "every push to main, and a run started by hand", **ONLY},
        }
    if event == "release":
        gate = "the gate of a release: the suite on the tagged commit before anything is published"
        return {
            "test": [],
            "e2e": True,
            "e2e-oldest": True,
            "overhead": False,
            "why": {
                "test": "the push of the release commit runs them",
                "e2e": gate,
                "e2e-oldest": gate,
                "overhead": "`make release` measures every runtime before it pushes anything",
            },
        }
    if event == "schedule":
        seen = [path for path in paths or [] if sees(path)]
        if repository.lower() == release_repository().lower():
            daily, why = False, "not in the repository releases are made in: each release runs the suite itself"
        elif paths == []:
            daily, why = False, f"main has not changed in {DAILY_HOURS} hours: the daily run before took it"
        elif paths is not None and not seen:
            daily, why = False, f"main changed in {DAILY_HOURS} hours only where the end-to-end job cannot see"
        elif paths is None:
            daily, why = True, "the daily run on main (what changed on it cannot be read)"
        else:
            more = f" and {len(seen) - 1} more" if len(seen) > 1 else ""
            daily, why = True, f"the daily run on main: {seen[0]}{more} changed"
        return {
            "test": [],
            "e2e": daily,
            "e2e-oldest": daily,
            "overhead": daily,
            "why": {
                "test": "every pull request and push to main runs them",
                "e2e": why,
                "e2e-oldest": why,
                "overhead": why,
            },
        }
    if event != "pull_request":
        why = dict.fromkeys(("test", "e2e", "e2e-oldest", "overhead"), f"every {event} event")
        return {"test": list(TEST_PARTS), "e2e": True, "e2e-oldest": True, "overhead": True, "why": why}
    if draft:
        later = "not for a draft, whatever its labels: they count once it is ready for review"
        return {
            "test": list(TEST_PARTS),
            "e2e": False,
            "e2e-oldest": False,
            "overhead": False,
            "why": {"test": "every pull request, a draft too", "e2e": later, "e2e-oldest": later, "overhead": later},
        }
    full = FULL_LABEL in labels
    benchmark = BENCHMARK_LABEL in labels
    return {
        "test": list(TEST_PARTS),
        "e2e": full,
        "e2e-oldest": full,
        "overhead": benchmark,
        "why": {
            "test": "every pull request",
            "e2e": "the run-full-ci label" if full else ONLY["e2e"],
            "e2e-oldest": "the run-full-ci label" if full else ONLY["e2e-oldest"],
            "overhead": "the benchmark label" if benchmark else ONLY["overhead"],
        },
    }


# --- reading the change -----------------------------------------------------------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - git with arguments of this script
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def changed(base: str, head: str | None = None) -> list[str]:
    """The files a change touches, from base to head or to the working tree and its new files; a renamed file twice."""
    paths = git("diff", "--name-only", "--no-renames", base, *([head] if head else [])).splitlines()
    if head is None:
        paths += git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({path for path in paths if path})


def first_parent() -> str:
    """The first parent of what CI checks out, a pull request's merge commit or a pushed commit (fetch-depth: 2)."""
    return git("rev-parse", "HEAD^1").strip()


def window_base(now: float | None = None) -> str | None:
    """The newest commit of main older than DAILY_HOURS, what the daily run before took (the daily run checks out the
    whole history); None when there is none."""
    cutoff = datetime.fromtimestamp((time.time() if now is None else now) - DAILY_HOURS * 3600, UTC)
    found = git("rev-list", "-1", f"--before={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}", "HEAD").strip()
    return found or None


def cells(base: str | None, head: str | None, environ=os.environ) -> str:
    """The cells of the Python qualification the change touches, as `make test-python-cells` takes them in CELLS: in the
    working tree (what CI checked out) against base, or against what qualify.select finds without one, or from base to
    head."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import qualify_python as qualify  # noqa: PLC0415 - only when the end-to-end job runs

    if head is None:
        environ = {**environ, **({"CELLS_BASE": base} if base else {})}
        return " ".join(qualify.select(environ))
    # the same as qualify.select, between two commits
    full: set[str] = set()
    canary: set[str] = set()
    for path in changed(base, head):
        own, shared = qualify.scope(path, qualify.content(base, path), qualify.content(head, path))
        full |= own
        canary |= shared
    found = sorted(full & set(qualify.LIBRARIES))
    found += sorted(f"{name}@{qualify.CANARY_PYTHON}" for name in canary - full)
    return " ".join(found)


# --- what the workflow reads ------------------------------------------------------------------------------------------


def outputs(jobs: dict, cells_selected: str) -> dict[str, str]:
    """The step outputs of the workflow's first job: the matrices of the jobs that run, empty for those that do not."""
    shards = kind_shards()

    def compact(value) -> str:
        return json.dumps(value, separators=(",", ":"))

    return {
        "test": compact(jobs["test"]),
        "images": str(jobs["e2e"] or jobs["e2e-oldest"]).lower(),
        "kind": compact(shards if jobs["e2e"] else []),
        "containers": compact(list(E2E_PARTS) if jobs["e2e"] else []),
        "cells": cells_selected if jobs["e2e"] else "",
        "oldest": compact(shards if jobs["e2e-oldest"] else []),
        "overhead": str(jobs["overhead"]).lower(),
    }


def report(jobs: dict, cells_selected: str | None) -> list[tuple[str, str, str]]:
    """(job, what runs of it, why) for each job."""
    shards = len(kind_shards())
    rows = []
    for job in ("test", "e2e", "e2e-oldest", "overhead"):
        what = "runs" if jobs[job] else "skipped"
        if job == "test" and jobs[job]:
            what += " (" + ", ".join(jobs["test"]) + ")"
        if job == "e2e" and jobs[job]:
            selected = "unknown without --base and --head" if cells_selected is None else cells_selected or "none"
            what += f" (images, kind 1-{shards}, {', '.join(E2E_PARTS)}; cells {selected})"
        if job == "e2e-oldest" and jobs[job]:
            what += f" (kind 1-{shards} on Kubernetes 1.27)"
        rows.append((job, what, jobs["why"][job]))
    return rows


def main(argv: list[str] | None = None, environ=os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--event", help="pull_request (the default outside a workflow), push, schedule or release")
    parser.add_argument("--label", action="append", default=[], help="a label of the pull request")
    parser.add_argument("--draft", action="store_true", help="the pull request is a draft")
    parser.add_argument("--base", help="the commit the change is made on")
    parser.add_argument("--head", help="the commit of the change (with --base)")
    parser.add_argument("--paths", help="a file listing the files the change touches, one a line; - for stdin")
    args = parser.parse_args(argv)

    in_workflow = environ.get("GITHUB_ACTIONS") == "true" and not (args.paths or args.base)
    event, labels, draft = args.event or "pull_request", args.label, args.draft
    repository = environ.get("GITHUB_REPOSITORY", "")
    if in_workflow:
        event = environ.get("GITHUB_EVENT_NAME", event)
        payload = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text()) if environ.get("GITHUB_EVENT_PATH") else {}
        pull = payload.get("pull_request") or {}
        labels = [label["name"] for label in pull.get("labels", [])]
        draft = bool(pull.get("draft"))
        if environ.get("CI_JOBS_RELEASE") == "true":  # the release workflow calls ci.yml for its gate
            event = "release"
        elif event == "workflow_dispatch":  # a run started by hand: what a push to main runs
            event = "push"

    paths: list[str] | None = []
    base: str | None = None
    head: str | None = None
    cells_selected: str | None = ""
    try:
        if args.paths:
            text = sys.stdin.read() if args.paths == "-" else Path(args.paths).read_text()
            paths = sorted({line.strip() for line in text.splitlines() if line.strip()})
            cells_selected = None
        elif args.base:
            base, head = args.base, args.head or "HEAD"
            paths = changed(base, head)
        elif event == "schedule":
            base = window_base()
            paths = changed(base, "HEAD") if base else None
        elif in_workflow:
            base = first_parent() if event == "pull_request" else None
            paths = changed(base) if base else []
        else:
            base = environ.get("CI_JOBS_BASE") or git("merge-base", "HEAD", "origin/main").strip()
            paths = changed(base)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"cannot read the change ({type(error).__name__})", file=sys.stderr)
        paths = None

    jobs = select(event, labels, paths, repository, draft=draft)
    if event == "release":
        cells_selected = ""  # `make release` runs every cell before it pushes anything
    elif jobs["e2e"] and cells_selected is not None:
        try:
            cells_selected = cells(base, head, environ)
        except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
            print(f"cannot tell which cells the change touches ({type(error).__name__}): every cell", file=sys.stderr)
            cells_selected = "all"

    rows = report(jobs, cells_selected)
    shown = event + (f", labels: {', '.join(labels)}" if labels else "") + (", a draft" if draft else "")
    print(f"{shown}; {len(paths) if paths is not None else 'unknown'} files changed")
    for job, what, why in rows:
        print(f"{job:<11} {what}: {why}")
    if environ.get("GITHUB_OUTPUT"):
        with Path(environ["GITHUB_OUTPUT"]).open("a") as out:
            for key, value in outputs(jobs, cells_selected or "").items():
                out.write(f"{key}={value}\n")
    if environ.get("GITHUB_STEP_SUMMARY"):
        with Path(environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write("| Job | Runs | Why |\n|---|---|---|\n")
            summary.writelines(f"| {job} | {what} | {why} |\n" for job, what, why in rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

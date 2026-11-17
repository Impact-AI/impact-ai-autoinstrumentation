# SPDX-License-Identifier: Apache-2.0

"""The workflows a public repository runs beside ci.yml and release.yml, and the newest Kubernetes shards of ci.yml.

Scorecard, dependency review and CodeQL grant nothing at the top and run where the repository is public (the Scorecard
API publishes public repositories only; dependency review and code scanning need GitHub Code Security in a private
one). Dependency review allows exactly the known vulnerabilities of the test fixtures that testing/apps/osv-scanner.toml
names with its reasons, so that a release's pull request fails only on a new one. The kind shards run on the newest
Kubernetes as on the oldest, so that the range the chart installs on is tested at both ends.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
JOBS = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]


def test_the_newest_shards_run_the_oldest_shards_on_the_newest_kind_node_image():
    oldest, newest = JOBS["e2e-oldest"], JOBS["e2e-newest"]
    assert (newest["needs"], newest["if"], newest["strategy"]) == (oldest["needs"], oldest["if"], oldest["strategy"])
    assert re.fullmatch(r"docker\.io/kindest/node:v1\.37\.\d+@sha256:[0-9a-f]{64}", newest["env"]["E2E_NODE_IMAGE"]), (
        "pinned by digest, as kind recommends"
    )
    assert [step.get("run") for step in newest["steps"]] == [step.get("run") for step in oldest["steps"]]


def test_the_supply_chain_workflows_grant_nothing_by_default_and_run_in_a_public_repository():
    for name in ("scorecard.yml", "dependency-review.yml", "codeql.yml"):
        workflow = yaml.safe_load((WORKFLOWS / name).read_text())
        assert workflow["permissions"] == {}, name
        for job in workflow["jobs"].values():
            assert job["if"] == "${{ !github.event.repository.private }}", name
            assert "write" not in job["permissions"].get("contents", "read"), name


def test_the_dependency_review_reads_its_settings_from_the_checked_out_file():
    checkout, review = yaml.safe_load((WORKFLOWS / "dependency-review.yml").read_text())["jobs"]["review"]["steps"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}
    assert review["uses"].startswith("actions/dependency-review-action@")
    assert review["with"] == {"config-file": "./.github/dependency-review-config.yml"}, "every setting is in the file"


def test_the_dependency_review_allows_exactly_the_known_vulnerabilities_of_the_test_fixtures():
    config = yaml.safe_load((ROOT / ".github" / "dependency-review-config.yml").read_text())
    entries = tomllib.loads((ROOT / "testing" / "apps" / "osv-scanner.toml").read_text())["IgnoredVulns"]
    ids = [entry["id"] for entry in entries]
    assert all(re.fullmatch(r"GHSA(-[0-9a-z]{4}){3}", ghsa) for ghsa in ids), (
        "GitHub's ids, the only ones dependency review allows; OSV-Scanner matches their aliases too"
    )
    assert len(set(ids)) == len(ids)
    assert all(entry.get("reason") and entry.get("ignoreUntil") for entry in entries), "each says why and until when"
    assert config["fail-on-severity"] == "moderate"
    assert sorted(config["allow-ghsas"]) == sorted(ids), "the dependency review allows what osv-scanner.toml names"


def test_go_is_installed_before_codeql_initializes_its_tracing_wrapper():
    steps = yaml.safe_load((WORKFLOWS / "codeql.yml").read_text())["jobs"]["analyze"]["steps"]
    install = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("jdx/mise-action@"))
    init = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("github/codeql-action/init@"))
    build = next(i for i, step in enumerate(steps) if step.get("run") == "go build ./...")
    assert install < init < build, "installing Go after init shadows CodeQL's tracing wrapper"

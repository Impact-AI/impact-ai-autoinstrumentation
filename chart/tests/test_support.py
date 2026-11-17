# SPDX-License-Identifier: Apache-2.0

"""SUPPORT.md and docs/compatibility.md say only what the repository shows.

The versions the compatibility page says CI runs, the Python versions it lists and the acknowledgement time SUPPORT.md
gives are read back from the files that decide them, so that a change to one of those files shows up as a failure here
until the page says it too. The versions taken from other projects (end of life dates) are checked against those
projects' own pages when the page is written; the commit that changes them cites the pages.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = (ROOT / "SUPPORT.md").read_text()
COMPATIBILITY = (ROOT / "docs" / "compatibility.md").read_text()
FLAT = " ".join(COMPATIBILITY.split())
README = (ROOT / "README.md").read_text()
SECURITY = (ROOT / "SECURITY.md").read_text()


def section(text: str, heading: str) -> str:
    body = text.split(f"\n## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def table_versions(runtime: str) -> list[str]:
    """The second cell of the rows of the runtimes table that belong to this runtime."""
    found, current = [], None
    for line in section(COMPATIBILITY, "Runtimes the kit instruments").splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Supported by the kit" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        current = cells[0] or current
        if current == runtime:
            found.append(cells[1])
    return found


def test_readme_and_the_supported_versions_of_security_md_link_to_it():
    assert "[SUPPORT.md](SUPPORT.md)" in README
    assert "[SUPPORT.md](SUPPORT.md)" in section(SECURITY, "Supported versions")


def test_the_python_versions_it_lists_are_the_ones_the_image_is_built_for():
    dockerfile = (ROOT / "python" / "Dockerfile").read_text()
    built = re.findall(r"python:(3\.\d+)-slim@sha256:[0-9a-f]{64} AS pyc3\d+", dockerfile)
    assert built == ["3.11", "3.12", "3.13", "3.14"]
    assert table_versions("Python") == built


def test_the_node_js_lines_it_lists_are_the_ones_the_qualification_covers_and_the_floor_is_the_trees():
    cells = json.loads((ROOT / "nodejs" / "qualification.json").read_text())["cells"]
    lines = sorted({cell["node"] for cell in cells})
    assert lines == [22, 24, 26]
    assert [row.split(",")[0] for row in table_versions("Node.js")] == [str(line) for line in lines]
    floor = re.search(r"const FLOOR = '([0-9.]+)';", (ROOT / "nodejs" / "src" / "impact" / "trees.js").read_text())
    assert floor.group(1) == "22.15"
    assert (
        f"Node.js {floor.group(1)} or later; on an older Node.js the application runs uninstrumented, and the setup "
        "report says why. Tested on 22, 24 and 26, CommonJS and ES modules."
    ) in FLAT


def test_no_document_claims_node_js_20():
    for name in ("README.md", "SUPPORT.md", "SECURITY.md", "CHANGELOG.md", "chart/impact-ai/README.md", "docs/*.md"):
        for path in sorted(ROOT.glob(name)):
            text = path.read_text()
            assert not re.search(r"Node\.js 20\b|\bnode2[0]\b", text), path


def test_the_kubernetes_versions_it_says_ci_runs_are_the_ones_ci_runs():
    kind = yaml.safe_load((ROOT / "e2e" / "kind.yaml").read_text())
    suite = re.search(r"kindest/node:v(\d+\.\d+)\.", kind["nodes"][0]["image"]).group(1)
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    # the Kubernetes part of the suite runs again on the oldest version (e2e-oldest) and on the newest (e2e-newest)
    oldest, newest = re.findall(r"E2E_NODE_IMAGE: docker.io/kindest/node:v(\d+\.\d+)\.", ci)
    chart = yaml.safe_load((ROOT / "chart" / "impact-ai" / "Chart.yaml").read_text())
    assert chart["kubeVersion"] == f">={oldest}.0-0", "the chart's floor is the oldest version CI runs"
    assert f"The chart installs on Kubernetes {oldest} and later" in FLAT
    assert f"Each release is tested on Kubernetes {oldest}, {suite} and {newest} before it is published" in FLAT
    assert f"the end-to-end suite on {suite} and its Kubernetes part on {oldest} and {newest}" in FLAT
    assert "and the code it is made from every day it changes" in FLAT
    # what shows it: the release workflow publishes nothing before ci.yml's suite passed on the tagged commit, and
    # ci.yml runs the suite every day (scripts/ci_jobs.py: when the day changed something it can see)
    release = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    assert release["jobs"]["publish"]["needs"] == "e2e"
    assert (release["jobs"]["e2e"]["uses"], release["jobs"]["e2e"]["with"]) == (
        "./.github/workflows/ci.yml",
        {"release": True},
    )
    assert re.search(r'^    - cron: "17 4 \* \* \*"', ci, re.MULTILINE), "the suite runs every day"
    # only what runs is said to run: no test of the oldest or the newest supported version is promised
    kubernetes = " ".join(section(COMPATIBILITY, "Kubernetes").split())
    for promise in ("the oldest and the newest", "Next step", "next step", "ends of the range"):
        assert promise not in kubernetes, promise


def test_the_security_section_gives_no_deadline_to_fix_and_the_acknowledgement_time_security_md_gives():
    fixes = section(SUPPORT, "Security fixes")
    assert re.findall(r"\b\d+ (?:business |calendar )?days\b", fixes) == ["3 business days"]
    assert "within 3 business days" in SECURITY
    for sentence in (
        "fixed without undue delay, the most severe and the actively exploited first",
        "Fixes go into the latest release",
        "announced as a GitHub security advisory, with a CVE where one applies",
        "disclosure is coordinated with the reporter",
        "documented with its reason (VEX)",
    ):
        assert sentence in " ".join(fixes.split()), sentence


def test_every_removal_is_announced_one_minor_release_ahead():
    assert "Any removal is announced one minor release ahead" in FLAT
    assert "the release before announces it" in FLAT

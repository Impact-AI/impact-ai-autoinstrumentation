# SPDX-License-Identifier: Apache-2.0

"""What Artifact Hub shows of a release: the copy of the chart a release stamps carries the version's changes from
CHANGELOG.md, the images it installs by digest, and whether it is a pre-release (scripts/stamp_chart.py --changelog).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import stamp_chart  # noqa: E402

PYTHON = "sha256:" + "1" * 64
REPORTER = "sha256:" + "2" * 64
NODEJS = "sha256:" + "3" * 64
CHANGELOG = """# Changelog

## [Unreleased]

## [0.3.0] - 2026-11-14

### Breaking

- The chart is named `impact-ai`: a new name
  on two lines.

### Added

- Something: with a colon, and "quotes".

### Fixed

- A fix.

## [0.2.1] - 2026-11-04

### Added

- Something older.
"""


def stamped(tmp_path: Path, version: str) -> dict:
    chart = tmp_path / "impact-ai"
    shutil.copytree(ROOT / "chart" / "impact-ai", chart)
    digests = {"python": PYTHON, "reporter": REPORTER, "nodejs": NODEJS}
    stamp_chart.stamp(chart, version, digests)
    stamp_chart.stamp_artifacthub(chart, version, digests, CHANGELOG)
    return yaml.safe_load((chart / "Chart.yaml").read_text())


def test_a_release_carries_its_changes_and_its_images_for_artifact_hub(tmp_path):
    annotations = stamped(tmp_path, "0.3.0")["annotations"]
    assert yaml.safe_load(annotations["artifacthub.io/changes"]) == [
        {"kind": "changed", "description": "Breaking: The chart is named `impact-ai`: a new name on two lines."},
        {"kind": "added", "description": 'Something: with a colon, and "quotes".'},
        {"kind": "fixed", "description": "A fix."},
    ]
    images = yaml.safe_load(annotations["artifacthub.io/images"])
    assert images == [
        {"name": "autoinstrumentation-python", "image": f"ghcr.io/impact-ai/autoinstrumentation-python:0.3.0@{PYTHON}"},
        {"name": "kubernetes-reporter", "image": f"ghcr.io/impact-ai/kubernetes-reporter:0.3.0@{REPORTER}"},
        {"name": "autoinstrumentation-nodejs", "image": f"ghcr.io/impact-ai/autoinstrumentation-nodejs:0.3.0@{NODEJS}"},
    ]
    assert "artifacthub.io/prerelease" not in annotations
    # what the committed chart says stays: the license and the links
    assert annotations["artifacthub.io/license"] == "Apache-2.0"
    assert {link["name"] for link in yaml.safe_load(annotations["artifacthub.io/links"])} == {
        "Documentation",
        "Support",
        "Security policy",
    }


def test_a_pre_release_says_so_and_a_version_without_a_section_lists_no_changes(tmp_path):
    annotations = stamped(tmp_path, "0.4.0-rc1")["annotations"]
    assert annotations["artifacthub.io/prerelease"] == "true"
    assert "artifacthub.io/changes" not in annotations
    assert "artifacthub.io/images" in annotations


def test_the_overhead_annotation_still_fits_after_artifact_hubs(tmp_path):
    chart = tmp_path / "impact-ai"
    shutil.copytree(ROOT / "chart" / "impact-ai", chart)
    digests = {"python": PYTHON, "reporter": REPORTER}
    stamp_chart.stamp(chart, "0.3.0", digests)
    stamp_chart.stamp_artifacthub(chart, "0.3.0", digests, CHANGELOG)
    results = {
        "schema": "impact.overhead.v1",
        "ok": True,
        "worst": {"cpuMsPerCall": 1.234, "memoryMiB": 12.34, "startSeconds": 0.567},
        "combos": [{}, {}],
    }
    stamp_chart.stamp_overhead(chart, results, "# Overhead\n")
    annotations = yaml.safe_load((chart / "Chart.yaml").read_text())["annotations"]
    assert (
        annotations["impact.ai/overhead"] == '{"cpuMsPerCall":1.23,"memoryMiB":12.3,"startSeconds":0.57,"workloads":2}'
    )
    assert len(yaml.safe_load(annotations["artifacthub.io/images"])) == 2
    assert len(yaml.safe_load(annotations["artifacthub.io/changes"])) == 3

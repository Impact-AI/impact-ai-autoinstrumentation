# SPDX-License-Identifier: Apache-2.0
"""The gate of `make scan` and `make release` holds: an image with a fixable vulnerability or an internal detail in its
files is refused, a clean one is not. Real trivy and the real container CLI, images made from nothing."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from containers import CONTAINER

ROOT = Path(__file__).resolve().parents[3]


def metadata(name: str, version: str) -> str:
    return f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"


# What each image holds, whether the scan must let it through, and what it must say when it does not.
CASES = {
    # urllib3 1.26.4 has HIGH vulnerabilities that were fixed in later releases
    "a package with a fixable HIGH vulnerability": (
        {"app/urllib3-1.26.4.dist-info/METADATA": metadata("urllib3", "1.26.4")},
        False,
        ["HIGH", "CVE-", "urllib3"],
    ),
    "an internal host in a file name": (
        {"home/build01.office" + ".lan" + "/notes.txt": ""},
        False,
        ["internal-host"],
    ),
    "a package no vulnerability is known for": (
        {"app/impact_scan_control-1.0.dist-info/METADATA": metadata("impact-scan-control", "1.0")},
        True,
        ["no fixable CRITICAL or HIGH vulnerability"],
    ),
}


def build(context: Path, files: dict[str, str]) -> str:
    for relative, text in files.items():
        target = context / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (context / "Containerfile").write_text("FROM scratch\nCOPY files/ /\n")
    tag = f"localhost/i019-scan-control/{uuid.uuid4().hex[:8]}:1"
    subprocess.run(  # noqa: S603 - the container CLI with test-controlled arguments
        [CONTAINER, "build", "-q", "-f", str(context / "Containerfile"), "-t", tag, str(context)],
        capture_output=True,
        check=True,
    )
    return tag


def scan(image: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - the repository's own script
        ["bash", str(ROOT / "scripts" / "scan-images.sh"), image],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        env={**os.environ, "CONTAINER": CONTAINER},
    )


@pytest.mark.image
def test_the_scan_refuses_what_it_must_and_lets_the_rest_through(tmp_path):
    # One test, not three: the first run downloads trivy's vulnerability database, and that should happen once.
    problems = []
    for number, (label, (files, passes, expected)) in enumerate(CASES.items()):
        context = tmp_path / str(number)
        context.mkdir()
        image = build(context, files)
        try:
            result = scan(image)
        finally:
            subprocess.run([CONTAINER, "rmi", "-f", image], capture_output=True, check=False)  # noqa: S603
        output = result.stdout + result.stderr
        if (result.returncode == 0) != passes:
            problems.append(f"{label}: exit {result.returncode}, expected {'0' if passes else 'a failure'}\n{output}")
        elif missing := [text for text in expected if text not in output]:
            problems.append(f"{label}: the output lacks {missing}\n{output}")
    assert not problems, "\n\n".join(problems)

# SPDX-License-Identifier: Apache-2.0
"""What the two images carry in /licenses: our LICENSE and NOTICE, and the license files of everything bundled."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from conftest import IMAGE, require_images
from containers import CONTAINER

REPORTER_IMAGE = os.environ.get("IMPACT_REPORTER_IMAGE", "localhost/impact-instrumentation/reporter:dev")
ROOT = Path(__file__).resolve().parents[3]


def licenses_archive(image: str) -> bytes:
    """/licenses as a tar stream, read out of the image without running it (the reporter's image has no shell)."""
    created = subprocess.run(  # noqa: S603
        [CONTAINER, "create", image], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        return subprocess.run(  # noqa: S603
            [CONTAINER, "cp", f"{created}:/licenses", "-"], capture_output=True, check=True
        ).stdout
    finally:
        subprocess.run([CONTAINER, "rm", created], capture_output=True, check=False)  # noqa: S603


def licenses_of(image: str) -> dict[str, bytes]:
    """The files of /licenses."""
    with tarfile.open(fileobj=io.BytesIO(licenses_archive(image))) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


@pytest.mark.image
@pytest.mark.parametrize("image", [IMAGE, REPORTER_IMAGE], ids=["python", "reporter"])
def test_any_user_can_open_every_directory_of_licenses_and_read_every_file(image):
    # The images run as a non-root user, and anyone may read what they carry. A directory without read and execute
    # for others hides its files: COPY --chmod onto a directory that COPY creates gives it the files' mode on some
    # builders (GitHub's Ubuntu 26.04 runners), so the Dockerfiles set modes in a build stage instead.
    require_images(image)
    with tarfile.open(fileobj=io.BytesIO(licenses_archive(image))) as tar:
        closed = [
            f"{m.name} {m.mode:04o}"
            for m in tar.getmembers()
            if (m.isdir() and (m.mode & 0o555) != 0o555) or (m.isfile() and (m.mode & 0o444) != 0o444)
        ]
    assert closed == []


@pytest.mark.image
def test_the_python_image_carries_our_license_and_notice_and_those_of_every_package_it_bundles():
    require_images(IMAGE)
    files = licenses_of(IMAGE)
    assert files["licenses/LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert files["licenses/NOTICE"] == (ROOT / "NOTICE").read_bytes()
    index = json.loads(files["licenses/python/index.json"])
    assert {
        "opentelemetry-sdk",
        "opentelemetry-instrumentation-genai-openai-agents",
        "wrapt",
        "impact-instrumentation",
    } <= {d["name"] for d in index}
    for entry in index:
        folder = f"licenses/python/{entry['name'].replace('-', '_')}-{entry['version']}"
        assert entry["files"] and all(f"{folder}/{name}" in files for name in entry["files"]), entry
    assert b"| Package | Version | License | Authors | Files |" in files["licenses/python/INDEX.md"]


@pytest.mark.image
def test_the_python_image_carries_the_license_of_the_busybox_it_runs_and_where_its_source_is():
    require_images(IMAGE)
    files = licenses_of(IMAGE)
    assert b"GNU GENERAL PUBLIC LICENSE" in files["licenses/busybox/COPYING"]
    assert b"https://busybox.net/downloads/busybox-1.37.0.tar.bz2" in files["licenses/busybox/README"]


@pytest.mark.image
def test_the_reporter_image_carries_our_license_and_notice_and_the_go_runtime_s():
    require_images(REPORTER_IMAGE)
    files = licenses_of(REPORTER_IMAGE)
    assert files["licenses/LICENSE"] == (ROOT / "LICENSE").read_bytes()
    assert files["licenses/NOTICE"] == (ROOT / "NOTICE").read_bytes()
    assert b"go (standard library and runtime)" in files["licenses/go/INDEX.md"]
    assert b"The Go Authors" in next(
        v for k, v in files.items() if k.startswith("licenses/go/go@go") and k.endswith("/LICENSE")
    )

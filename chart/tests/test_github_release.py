# SPDX-License-Identifier: Apache-2.0

"""The GitHub release of a version: its notes come from CHANGELOG.md, its files are the chart, the bills of materials,
LICENSE and NOTICE with SHA256SUMS and its signature, and it is published from a draft.

scripts/github-release.sh runs here in a copy of the scripts it needs, with cosign and gh replaced by stand-ins that
write down how they were called; scripts/release_notes.py runs as it is.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("github-release.sh", "release_notes.py", "lib.sh", "names.sh")
REPOSITORY = "Impact-AI/impact-ai-autoinstrumentation"
DIGEST = "sha256:" + "a" * 64
CHANGELOG = """# Changelog

## [Unreleased]

## [0.3.0] - 2026-11-14

### Added

- Something new.

## [0.3.0-rc1] - 2026-11-05

### Fixed

- Something old.
"""
COSIGN = """#!/bin/sh
echo "cosign $*" >>"$CALLS"
if [ "$1" = sign-blob ]; then
  while [ $# -gt 0 ]; do
    if [ "$1" = --bundle ]; then echo '{"bundle": true}' >"$2"; fi
    shift
  done
fi
exit 0
"""
GH = """#!/bin/sh
echo "gh $*" >>"$CALLS"
exit 0
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of what the script reads: the scripts, LICENSE, NOTICE, CHANGELOG.md and what make release wrote."""
    root = tmp_path / "repository"
    (root / "scripts").mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    (root / "LICENSE").write_text("License\n")
    (root / "NOTICE").write_text("Notice\n")
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    release = root / "build" / "release"
    release.mkdir(parents=True)
    for number in ("0.3.0", "0.3.0-rc1"):
        (release / f"impact-ai-{number}.tgz").write_bytes(b"chart " + number.encode())
    (release / "chart.name").write_text("ghcr.io/impact-ai/charts/impact-ai\n")
    (release / "chart.digest").write_text(DIGEST + "\n")
    for key, image in (
        ("python", "autoinstrumentation-python"),
        ("nodejs", "autoinstrumentation-nodejs"),
        ("reporter", "kubernetes-reporter"),
    ):
        (release / f"{key}.name").write_text(f"ghcr.io/impact-ai/{image}\n")
        (release / f"{key}.digest").write_text(DIGEST + "\n")
        for arch in ("amd64", "arm64"):
            (release / f"{key}-{arch}.spdx.json").write_text(f'{{"name": "{image} {arch}"}}\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("cosign", COSIGN), ("gh", GH)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return root


def run(tree: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    environment = {
        **os.environ,
        "PATH": f"{tree.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(tree.parent / "calls"),
        "GITHUB_REPOSITORY": REPOSITORY,
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.example.test",  # keyless, as in the release workflow
        **env,
    }
    environment.pop("COSIGN_KEY", None)
    return subprocess.run(  # noqa: S603
        ["bash", str(tree / "scripts" / "github-release.sh"), *args],  # noqa: S607
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def calls(tree: Path) -> list[str]:
    path = tree.parent / "calls"
    return path.read_text().splitlines() if path.exists() else []


def test_prepare_lists_the_files_of_the_release_with_their_sha256_and_signs_the_list(tree):
    result = run(tree, "prepare", "v0.3.0")
    assert result.returncode == 0, result.stderr
    files = tree / "build" / "release" / "github"
    assert sorted(p.name for p in files.iterdir()) == sorted(
        [
            "impact-ai-0.3.0.tgz",
            *(
                f"{image}-0.3.0-linux-{arch}.spdx.json"
                for image in ("autoinstrumentation-python", "autoinstrumentation-nodejs", "kubernetes-reporter")
                for arch in ("amd64", "arm64")
            ),
            "LICENSE",
            "NOTICE",
            "SHA256SUMS",
            "SHA256SUMS.sigstore.json",
        ]
    )
    sums = dict(reversed(line.split("  ", 1)) for line in (files / "SHA256SUMS").read_text().splitlines())
    assert set(sums) == {p.name for p in files.iterdir()} - {"SHA256SUMS", "SHA256SUMS.sigstore.json"}
    for name, digest in sums.items():
        assert hashlib.sha256((files / name).read_bytes()).hexdigest() == digest, name
    sign, verify = calls(tree)
    assert sign.startswith("cosign sign-blob --yes --bundle ") and sign.endswith("/SHA256SUMS")
    identity = f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/v0.3.0"
    assert verify.startswith("cosign verify-blob ") and f"--certificate-identity {identity}" in verify


def test_the_notes_are_the_changelog_section_what_was_published_and_how_to_verify_it(tree):
    assert run(tree, "prepare", "0.3.0").returncode == 0
    notes = (tree / "build" / "release" / "NOTES.md").read_text()
    assert notes.startswith("### Added\n\n- Something new.\n")
    assert "Something old" not in notes and "Unreleased" not in notes
    assert f"`ghcr.io/impact-ai/autoinstrumentation-python:0.3.0@{DIGEST}`" in notes
    assert f"`oci://ghcr.io/impact-ai/charts/impact-ai` version `0.3.0`, `{DIGEST}`" in notes
    assert "release.yml@refs/tags/v0.3.0" in notes and "sha256sum --check --ignore-missing SHA256SUMS" in notes


@pytest.mark.parametrize("changelog", ["## [Unreleased]\n\n## [0.3.0]\n\n- Not dated.\n", "## [0.2.1] - 2026-11-04\n"])
def test_a_version_without_a_dated_changelog_section_is_not_released(tree, changelog):
    (tree / "CHANGELOG.md").write_text("# Changelog\n\n" + changelog)
    result = run(tree, "prepare", "0.3.0")
    assert result.returncode != 0
    assert "CHANGELOG.md has no dated section '## [0.3.0] - YYYY-MM-DD'" in result.stderr
    assert calls(tree) == [], "nothing is signed for a release without notes"


@pytest.mark.parametrize(("version", "prerelease"), [("0.3.0", False), ("0.3.0-rc1", True)])
def test_publish_makes_a_draft_with_the_files_then_publishes_it(tree, version, prerelease):
    assert run(tree, "prepare", version).returncode == 0
    bundle = tree.parent / "attestation.json"
    bundle.write_text("{}\n")
    result = run(tree, "publish", version, PROVENANCE_BUNDLE=str(bundle))
    assert result.returncode == 0, result.stderr
    create, edit = calls(tree)[2:]
    assert create.startswith(f"gh release create v{version} --repo {REPOSITORY} --draft --verify-tag --title {version}")
    assert ("--prerelease" in create) is prerelease
    assert "/provenance.sigstore.json" in create and "/SHA256SUMS.sigstore.json" in create
    assert edit == f"gh release edit v{version} --repo {REPOSITORY} --draft=false"


def test_publish_refuses_files_that_were_not_prepared(tree):
    result = run(tree, "publish", "0.3.0")
    assert result.returncode != 0 and "run prepare first" in result.stderr
    assert calls(tree) == []


def test_release_notes_runs_alone_and_fails_without_a_section(tree):
    release = tree / "build" / "release"
    ok = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "release_notes.py"),
            "v0.3.0-rc1",
            str(release),
            "--changelog",
            str(tree / "CHANGELOG.md"),
            "--repository",
            REPOSITORY,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0 and ok.stdout.startswith("### Fixed\n\n- Something old.\n"), ok.stderr
    missing = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "release_notes.py"),
            "v9.9.9",
            str(release),
            "--changelog",
            str(tree / "CHANGELOG.md"),
            "--repository",
            REPOSITORY,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 1 and "no dated section '## [9.9.9]" in missing.stderr

# SPDX-License-Identifier: Apache-2.0

"""The gates around a release and a contribution: a tag that is not on main publishes nothing
(scripts/release-reviewed.sh), and every commit of a pull request is signed off by its author (scripts/check_dco.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AUTHOR = {"GIT_AUTHOR_NAME": "A Contributor", "GIT_AUTHOR_EMAIL": "contributor@example.test"}
COMMITTER = {"GIT_COMMITTER_NAME": "A Contributor", "GIT_COMMITTER_EMAIL": "contributor@example.test"}


def git(*args: str, cwd: Path, **env: str) -> str:
    environment = {**os.environ, **AUTHOR, **COMMITTER, "GIT_CONFIG_GLOBAL": os.devnull, **env}
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=environment,  # noqa: S607
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git("init", "-q", "-b", "main", cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "The last release", cwd=tmp_path)
    return tmp_path


def reviewed(repository: Path, commit: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "GITHUB_SHA": commit, "RELEASE_MAIN": "main"}
    return subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts" / "release-reviewed.sh")],  # noqa: S607
        cwd=repository,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def test_a_tag_on_a_release_branch_that_was_not_merged_publishes_nothing_and_once_merged_it_does(repository):
    git("checkout", "-q", "-b", "release/0.3.0", cwd=repository)
    git("commit", "-q", "--allow-empty", "-m", "Release 0.3.0", cwd=repository)
    release = git("rev-parse", "HEAD", cwd=repository)
    refused = reviewed(repository, release)
    assert refused.returncode == 1 and "was not merged" in refused.stderr
    git("checkout", "-q", "main", cwd=repository)
    git("merge", "-q", "--no-ff", "-m", "Merge pull request #1 from release/0.3.0", "release/0.3.0", cwd=repository)
    passed = reviewed(repository, release)
    assert passed.returncode == 0, passed.stderr


def test_without_the_history_of_main_the_gate_says_so(repository):
    environment = {**os.environ, "GITHUB_SHA": git("rev-parse", "HEAD", cwd=repository), "RELEASE_MAIN": "origin/main"}
    result = subprocess.run(  # noqa: S603
        ["bash", str(ROOT / "scripts" / "release-reviewed.sh")],  # noqa: S607
        cwd=repository,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 1 and "fetch-depth: 0" in result.stderr


def dco(repository: Path, base: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "check_dco.py"), base, "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def test_commits_signed_off_by_their_author_pass(repository):
    base = git("rev-parse", "HEAD", cwd=repository)
    git("commit", "-q", "--allow-empty", "-s", "-m", "A fix", cwd=repository)
    git(
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "Another\n\nSigned-off-by: A Contributor <CONTRIBUTOR@example.test>",
        cwd=repository,
    )
    result = dco(repository, base)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "message",
    [
        "A fix without a sign-off",
        "A fix\n\nSigned-off-by: Someone Else <someone@example.test>",
        "A fix\n\nSigned-off-by: me",
    ],
)
def test_a_commit_without_its_authors_sign_off_fails_and_is_named(repository, message):
    base = git("rev-parse", "HEAD", cwd=repository)
    git("commit", "-q", "--allow-empty", "-s", "-m", "A signed fix", cwd=repository)
    git("commit", "-q", "--allow-empty", "-m", message, cwd=repository)
    unsigned = git("rev-parse", "HEAD", cwd=repository)
    result = dco(repository, base)
    assert result.returncode == 1
    assert f"{unsigned[:12]} by contributor@example.test" in result.stderr
    assert result.stderr.count("check-dco: ") == 2, "one line for the commit, one with the way out"


def test_a_merge_commit_is_not_checked(repository):
    base = git("rev-parse", "HEAD", cwd=repository)
    git("checkout", "-q", "-b", "topic", cwd=repository)
    git("commit", "-q", "--allow-empty", "-s", "-m", "A signed fix", cwd=repository)
    git("checkout", "-q", "main", cwd=repository)
    git("commit", "-q", "--allow-empty", "-s", "-m", "Another signed fix", cwd=repository)
    git("merge", "-q", "--no-ff", "-m", "Merge topic", "topic", cwd=repository)
    result = dco(repository, base)
    assert result.returncode == 0, result.stderr

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The notes of a version's GitHub release: its section of CHANGELOG.md, what it published, and how to verify that.

    python3 scripts/release_notes.py <version> <release dir> [--changelog CHANGELOG.md] [--repository OWNER/REPO]
    python3 scripts/release_notes.py <version> --changes                 the changelog section alone (a release's PR)

<release dir> is what make release wrote: <key>.name and <key>.digest of each image it pushed, and chart.name and
chart.digest. The repository is the one whose release workflow signed them (GITHUB_REPOSITORY, else
RELEASE_REPOSITORY of scripts/names.sh). It exits 1 when CHANGELOG.md has no dated section for the version: a release
says what changed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ("python", "nodejs", "java", "reporter")  # in this order, where the release pushed them


def section(changelog: str, number: str) -> str:
    """The body of '## [<number>] - <date>', without its heading; ValueError when there is none."""
    heading = rf"^## \[{re.escape(number)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n"
    match = re.search(heading + r"(.*?)(?=^## \[|\Z)", changelog, re.MULTILINE | re.DOTALL)
    if not match or not match.group(1).strip():
        raise ValueError(f"CHANGELOG.md has no dated section '## [{number}] - YYYY-MM-DD' with what changed")
    return match.group(1).strip()


def release_repository() -> str:
    names = (ROOT / "scripts" / "names.sh").read_text()
    return re.search(r"^RELEASE_REPOSITORY=(\S+)$", names, re.MULTILINE).group(1)


def notes(number: str, changelog: str, release: Path, repository: str) -> str:
    rows = []
    chart = (release / "chart.name").read_text().strip()
    rows.append(f"| Chart | `oci://{chart}` version `{number}`, `{(release / 'chart.digest').read_text().strip()}` |")
    for key in IMAGES:
        if (release / f"{key}.digest").is_file():
            name = (release / f"{key}.name").read_text().strip()
            digest = (release / f"{key}.digest").read_text().strip()
            rows.append(f"| {name.rsplit('/', 1)[1]} | `{name}:{number}@{digest}` |")
    identity = f"https://github.com/{repository}/.github/workflows/release.yml@refs/tags/v{number}"
    return "\n".join(
        [
            section(changelog, number),
            "",
            "## What this release published",
            "",
            "| | Reference |",
            "|---|---|",
            *rows,
            "",
            "## Verify it",
            "",
            "The chart and every image are signed with cosign by this repository's release workflow, each image with a "
            "signed SPDX bill of materials for each platform and GitHub build provenance. The files of this release "
            "are listed with their SHA-256 in `SHA256SUMS`, which `SHA256SUMS.sigstore.json` signs:",
            "",
            "```sh",
            "cosign verify-blob SHA256SUMS --bundle SHA256SUMS.sigstore.json \\",
            f'  --certificate-identity "{identity}" \\',
            "  --certificate-oidc-issuer https://token.actions.githubusercontent.com",
            "sha256sum --check --ignore-missing SHA256SUMS",
            "```",
            "",
            "The chart and the images, by digest: [verify what you install]"
            f"(https://github.com/{repository}/blob/v{number}/chart/impact-ai/README.md#verify-what-you-install).",
            "",
        ]
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("version")
    parser.add_argument("release", type=Path, nargs="?")
    parser.add_argument("--changes", action="store_true", help="only the version's section of the changelog")
    parser.add_argument("--changelog", type=Path, default=ROOT / "CHANGELOG.md")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY") or release_repository())
    args = parser.parse_args(argv)
    number = args.version.removeprefix("v")
    if not args.changes and args.release is None:
        parser.error("the release directory is needed unless --changes")
    try:
        changelog = args.changelog.read_text()
        text = (
            section(changelog, number) + "\n"
            if args.changes
            else notes(number, changelog, args.release, args.repository)
        )
        sys.stdout.write(text)
    except (ValueError, OSError) as error:
        print(f"release-notes: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

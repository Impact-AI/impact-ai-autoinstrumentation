#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Stamp a copy of the chart with a release: version, the digests of the images the release built, and its overhead.

    stamp_chart.py CHART_DIR VERSION --python sha256:... --reporter sha256:... [--nodejs sha256:...] [--java sha256:...]
    [--overhead results.json] [--repository python=ghcr.io/impact-ai/autoinstrumentation-python ...]

The committed chart carries no version of its own and leaves the digests of the kit's own images empty, so a chart that
was not built by a release cannot be installed by accident. The release writes both before it packages the chart, and
the repository of each image it pushed (--repository), which is the committed default when the release publishes at the
root of the public organisation.

The chart README links to the documentation of the public repository at `blob/main`; the copy links to the tag of the
release instead (`blob/v<version>`), so that what `helm show readme` and Artifact Hub show reads the documents of the
version installed.

--changelog names CHANGELOG.md: the copy's Chart.yaml then carries what Artifact Hub shows of a release, the
version's changes (artifacthub.io/changes, from its section of the changelog), the images it installs by digest
(artifacthub.io/images, which Artifact Hub scans), and artifacthub.io/prerelease for a pre-release.

--overhead takes the results of the release's overhead measurements (scripts/overhead_report.py) and puts them into the
copy of the chart: OVERHEAD.md beside Chart.yaml, and the worst numbers as the annotation impact.ai/overhead of
Chart.yaml, so that every release publishes what it costs. The numbers are measured by the release itself and
are never committed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# Where the chart README links to the documents of the public repository (scripts/names.sh, RELEASE_REPOSITORY).
DOCUMENTS = "https://github.com/Impact-AI/impact-autoinstrumentation/blob/"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")


def stamp(chart: Path, version: str, digests: dict[str, str], repositories: dict[str, str] | None = None) -> None:
    """Version, digests and, when given, the repositories the images are pulled from (the release pushes to a root of
    its own; the defaults of the committed chart are the root of the public organisation)."""
    if not SEMVER.match(version):
        raise SystemExit(f"{version!r} is not a semantic version")
    for name, digest in digests.items():
        if not DIGEST.match(digest):
            raise SystemExit(f"{name}: {digest!r} is not a sha256 digest")

    chart_yaml = chart / "Chart.yaml"
    text = chart_yaml.read_text()
    for key in ("version", "appVersion"):
        text, count = re.subn(rf"(?m)^{key}: .*$", f"{key}: {version}", text, count=1)
        if count != 1:
            raise SystemExit(f"Chart.yaml has no {key}")
    chart_yaml.write_text(text)

    readme = chart / "README.md"
    if readme.exists():
        readme.write_text(readme.read_text().replace(DOCUMENTS + "main/", f"{DOCUMENTS}v{version}/"))

    values_yaml = chart / "values.yaml"
    text = values_yaml.read_text()
    for name, digest in digests.items():
        pattern = rf'(?m)^(  {name}:\n    repository: .*\n)    tag: ""\n    digest: ""$'
        text, count = re.subn(pattern, rf'\1    tag: "{version}"\n    digest: {digest}', text, count=1)
        if count != 1:
            raise SystemExit(f"values.yaml: no empty tag and digest for images.{name}")
    for name, repository in (repositories or {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9.:/_-]*", repository):
            raise SystemExit(f"images.{name}: {repository!r} is not a lowercase image repository")
        text, count = re.subn(rf"(?m)^(  {name}:\n    repository: ).*$", rf"\g<1>{repository}", text, count=1)
        if count != 1:
            raise SystemExit(f"values.yaml: no repository for images.{name}")
    values_yaml.write_text(text)


# The kinds of change Artifact Hub knows, by the heading of a changelog section; a breaking change is a change, said so.
CHANGE_KINDS = {
    "Breaking": "changed",
    "Added": "added",
    "Changed": "changed",
    "Deprecated": "deprecated",
    "Removed": "removed",
    "Fixed": "fixed",
    "Security": "security",
}


def changes(changelog: str, version: str) -> list[tuple[str, str]]:
    """(kind, description) of each entry of the version's section of the changelog, in order; [] without a section."""
    match = re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## \[|\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    found, kind = [], None
    for block in re.split(r"(?m)^(?=### |- )", match.group(1)):
        if block.startswith("### "):
            heading = block[4:].split("\n", 1)[0].strip()
            kind = heading if heading in CHANGE_KINDS else None
        elif block.startswith("- ") and kind:
            text = " ".join(block[2:].split())
            found.append((CHANGE_KINDS[kind], f"Breaking: {text}" if kind == "Breaking" else text))
    return found


def stamp_artifacthub(chart: Path, version: str, digests: dict[str, str], changelog: str) -> None:
    """The annotations Artifact Hub reads of a release: its changes, its images by digest, and whether it is a
    pre-release. The images are those the release built, from the stamped values.yaml."""
    values = (chart / "values.yaml").read_text()
    lines = ["  artifacthub.io/images: |"]
    for name, digest in digests.items():
        repository = re.search(rf"(?m)^  {name}:\n    repository: (\S+)$", values).group(1)
        lines += [f"    - name: {repository.rsplit('/', 1)[1]}", f"      image: {repository}:{version}@{digest}"]
    found = changes(changelog, version)
    if found:
        lines.append("  artifacthub.io/changes: |")
        for kind, description in found:
            lines += [f"    - kind: {kind}", f"      description: {json.dumps(description)}"]
    if "-" in version:
        lines.append('  artifacthub.io/prerelease: "true"')
    chart_yaml = chart / "Chart.yaml"
    text, count = re.subn(
        r"(?m)^(annotations:\n(?:  .*\n)*)",
        lambda m: m.group(1) + "\n".join(lines) + "\n",
        chart_yaml.read_text(),
        count=1,
    )
    if count != 1:
        raise SystemExit("Chart.yaml has no annotations to put Artifact Hub's in")
    chart_yaml.write_text(text)


def stamp_overhead(chart: Path, results: dict, report: str) -> None:
    """OVERHEAD.md and the annotation impact.ai/overhead (a JSON object on one line) in the copy of the chart."""
    if results.get("schema") != "impact.overhead.v1":
        raise SystemExit(f"overhead results with the schema {results.get('schema')!r}, expected impact.overhead.v1")
    if not results.get("ok"):
        raise SystemExit("the overhead is over the budget: a release does not publish it")
    chart_yaml = chart / "Chart.yaml"
    text = chart_yaml.read_text()
    if "impact.ai/overhead:" in text:
        raise SystemExit("Chart.yaml already carries an impact.ai/overhead annotation")
    worst = results["worst"]
    value = json.dumps(
        {
            "cpuMsPerCall": round(worst["cpuMsPerCall"], 2),
            "memoryMiB": round(worst["memoryMiB"], 1),
            "startSeconds": round(worst["startSeconds"], 2),
            "workloads": len(results["combos"]),
        },
        separators=(",", ":"),
    )
    text, count = re.subn(r"(?m)^(annotations:\n(?:  .*\n)*)", rf"\1  impact.ai/overhead: '{value}'\n", text, count=1)
    if count != 1:
        raise SystemExit("Chart.yaml has no annotations to put the overhead in")
    chart_yaml.write_text(text)
    (chart / "OVERHEAD.md").write_text(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("chart", type=Path)
    parser.add_argument("version")
    parser.add_argument("--python", required=True, help="digest of the python image")
    parser.add_argument("--reporter", required=True, help="digest of the reporter image")
    parser.add_argument(
        "--nodejs", help="digest of the Node.js image (the chart's values.yaml must have its empty block)"
    )
    parser.add_argument("--java", help="digest of the Java image (the chart's values.yaml must have its empty block)")
    parser.add_argument(
        "--overhead", type=Path, help="results.json of scripts/overhead_report.py; OVERHEAD.md is beside it"
    )
    parser.add_argument("--changelog", type=Path, help="CHANGELOG.md: Artifact Hub's annotations of the release")
    parser.add_argument(
        "--repository",
        action="append",
        default=[],
        metavar="KEY=REPOSITORY",
        help="where an image is pulled from, for example python=ghcr.io/impact-ai/autoinstrumentation-python",
    )
    args = parser.parse_args(argv)
    digests = {"python": args.python, "reporter": args.reporter}
    if args.nodejs:
        digests["nodejs"] = args.nodejs
    if args.java:
        digests["java"] = args.java
    repositories = dict(item.split("=", 1) for item in args.repository)
    stamp(args.chart, args.version, digests, repositories)
    if args.changelog:
        stamp_artifacthub(args.chart, args.version, digests, args.changelog.read_text())
    if args.overhead:
        stamp_overhead(
            args.chart, json.loads(args.overhead.read_text()), (args.overhead.parent / "OVERHEAD.md").read_text()
        )
    print(f"stamped {args.chart} as {args.version}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

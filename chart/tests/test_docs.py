# SPDX-License-Identifier: Apache-2.0

"""The documents a reader of the public repository meets: their links resolve, and the first screens say what matters.

Every link of a Markdown file that points into this repository, relatively or at its `blob/main` on GitHub, has to name
a file the tree holds, and a `#fragment` has to name a heading of the page it points at. The chart README is also what
`helm show readme` and Artifact Hub display from the packaged chart, where only the chart directory exists: it links
into the rest of the repository by absolute URL only (scripts/stamp_chart.py points those at the tag of the release).
Both READMEs say on their first screen that prompts and answers are captured by default, and the setting that turns
that off.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "chart" / "impact-ai"
REPOSITORY = "https://github.com/Impact-AI/impact-autoinstrumentation/blob/main/"
SWITCH = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT"
INLINE = re.compile(r"\]\(([^)\s]+)\)")
REFERENCE = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)


def markdown_files() -> list[Path]:
    out = subprocess.run(  # noqa: S603 - fixed command
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.md"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / name for name in out.split("\0") if name and (ROOT / name).is_file()]


def prose(text: str) -> str:
    """The text without its fenced code blocks and inline code, where a bracket is code and not a link."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def links(path: Path) -> list[str]:
    text = prose(path.read_text())
    return INLINE.findall(text) + REFERENCE.findall(text)


def anchors(path: Path) -> set[str]:
    """The fragments GitHub gives the headings of a Markdown page."""
    found = set()
    for heading in re.findall(r"^#{1,6} +(.+?) *$", prose(path.read_text()), re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        found.add(slug)
    return found


def target(source: Path, link: str) -> tuple[Path, str] | None:
    """The file of this repository a link points at, and its fragment; None for a link elsewhere."""
    if link.startswith(REPOSITORY):
        path, _, fragment = link[len(REPOSITORY) :].partition("#")
        return ROOT / path, fragment
    if re.match(r"^[a-z][a-z0-9+.-]*:", link) or link.startswith("//"):
        return None
    path, _, fragment = link.partition("#")
    return (source.parent / path if path else source).resolve(), fragment


DOCUMENTS = markdown_files()


@pytest.mark.parametrize("source", DOCUMENTS, ids=[str(p.relative_to(ROOT)) for p in DOCUMENTS])
def test_every_link_into_the_repository_names_a_file_it_holds_and_a_heading_of_that_page(source):
    broken = []
    for link in links(source):
        found = target(source, link)
        if found is None:
            continue
        path, fragment = found
        if not path.exists():
            broken.append(f"{link}: no {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
        elif fragment and path.suffix == ".md" and fragment not in anchors(path):
            broken.append(f"{link}: no heading #{fragment}")
    assert broken == []


def test_the_chart_readme_links_out_of_the_chart_only_by_absolute_url():
    readme = CHART / "README.md"
    outside = []
    for link in links(readme):
        found = target(readme, link)
        if found and not link.startswith(REPOSITORY) and not found[0].is_relative_to(CHART):
            outside.append(link)
    assert outside == [], "a relative link out of the chart breaks in the packaged chart"
    assert REPOSITORY + "docs/" in readme.read_text()


def test_the_chart_readme_names_no_test_of_this_repository():
    # The packaged chart's README is for whoever installs it; how the kit is tested is in docs/ and CONTRIBUTING.md.
    readme = (CHART / "README.md").read_text()
    for maintainer in ("chart/tests/", "python/tests/", "nodejs/tests/", "e2e/test_", "make test-", "make qualify"):
        assert maintainer not in readme, maintainer


@pytest.mark.parametrize("readme", [ROOT / "README.md", CHART / "README.md"], ids=["README.md", "chart README"])
def test_the_first_screen_says_content_is_captured_by_default_and_how_to_turn_it_off(readme):
    first_screen = readme.read_text().split("\n## ", 1)[0]
    flat = " ".join(first_screen.split())
    assert "by default" in flat and "prompt" in flat and "answer" in flat
    assert SWITCH in flat
    assert len(first_screen.splitlines()) <= 25, "the first screen fits a screen"


def test_the_root_readme_opens_with_install_verify_and_a_documentation_index():
    readme = (ROOT / "README.md").read_text()
    headings = re.findall(r"^## (.+)$", readme, re.MULTILINE)
    assert headings[:5] == ["What it installs", "Requirements", "Install", "Verify a release", "Documentation"]
    install = readme.split("## Install\n", 1)[1]
    assert "helm upgrade --install impact-ai oci://ghcr.io/impact-ai/charts/impact-ai --version <version>" in install


def test_the_community_files_are_there():
    for name in (
        "README.md",
        "LICENSE",
        "NOTICE",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ):
        assert (ROOT / name).is_file(), name


def test_the_code_of_conduct_is_the_contributor_covenant_with_the_contact_security_md_gives():
    conduct = (ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert conduct.startswith("# Contributor Covenant Code of Conduct\n")
    assert "version 2.1" in conduct and "[INSERT CONTACT METHOD]" not in conduct
    contact = re.search(r"\* By e-mail: (\S+@\S+)", (ROOT / "SECURITY.md").read_text()).group(1)
    assert f"responsible for enforcement at {contact}." in conduct


def test_the_changelog_keeps_its_format():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    versions = re.findall(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?$", changelog, re.MULTILINE)
    assert versions[0] == ("Unreleased", "")
    dated = [date for _, date in versions[1:]]
    assert all(dated) and dated == sorted(dated, reverse=True), "each release dated, the newest first"
    sections = set(re.findall(r"^### (.+)$", changelog, re.MULTILINE))
    assert sections <= {"Breaking", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"}


def test_the_issue_forms_ask_for_what_a_report_needs():
    bug = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug.yml").read_text()
    for needed in ("helm list -n impact-ai", "kubectl -n impact-ai get pods", "impact-ai-reporter", "SECURITY.md"):
        assert needed in bug, needed
    config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text()
    assert "blank_issues_enabled: false" in config and "security/advisories/new" in config

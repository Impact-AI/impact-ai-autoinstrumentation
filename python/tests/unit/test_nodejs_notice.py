# SPDX-License-Identifier: Apache-2.0
"""The notice attributes what the Node.js image bundles, and the chart's copy of it is the same file."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
NOTICE = (ROOT / "NOTICE").read_text()


@pytest.mark.parametrize(
    "component",
    [
        "OpenTelemetry JavaScript",
        "OpenInference",
        "import-in-the-middle",
        "require-in-the-middle",
        "cjs-module-lexer",
        "es-module-lexer",
        "shimmer",
        "/licenses/nodejs/INDEX.md",
    ],
)
def test_the_notice_attributes_what_the_nodejs_image_bundles(component):
    assert component in NOTICE


def test_the_notice_passes_on_the_notice_of_a_bundled_package_that_has_one():
    # import-in-the-middle ships a NOTICE that Apache 2.0 section 4(d) passes on.
    assert "This product includes software developed at Datadog (https://www.datadoghq.com/)." in NOTICE


def test_the_notice_says_that_the_package_called_openai_is_not_openais():
    assert "stand-in written for this repository" in NOTICE


def test_the_notice_of_the_chart_is_this_one():
    assert (ROOT / "chart" / "impact-ai" / "NOTICE").read_text() == NOTICE

# SPDX-License-Identifier: Apache-2.0
"""The kit is Apache-2.0: the license text, the notice and the copies in the chart."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "chart" / "impact-ai"
# SHA-256 of https://www.apache.org/licenses/LICENSE-2.0.txt
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def test_the_license_is_the_canonical_apache_2_0_text():
    text = (ROOT / "LICENSE").read_bytes()
    assert hashlib.sha256(text).hexdigest() == APACHE_2_SHA256
    assert b"Apache License\n                           Version 2.0, January 2004" in text


def test_the_notice_names_the_project_the_year_and_the_authors():
    lines = (ROOT / "NOTICE").read_text().splitlines()
    assert lines[:2] == ["Impact AI cluster kit", "Copyright 2026 The Impact AI Authors"]


@pytest.mark.parametrize(
    "component",
    [
        "OpenTelemetry Operator Helm chart",
        "OpenTelemetry Python",
        "OpenLLMetry",
        "wrapt",
        "packaging",
        "asgiref",
        "typing_extensions",
        "Go runtime and standard library",
        "BusyBox",
        "distroless",
    ],
)
def test_the_notice_attributes_what_is_bundled(component):
    assert component in (ROOT / "NOTICE").read_text()


def test_the_notice_carries_the_notice_of_a_bundled_package_that_has_one():
    # opentelemetry-instrumentation-genai-langchain ships a NOTICE that Apache 2.0 section 4(d) passes on.
    assert "derived from Traceloop OpenLLMetry" in (ROOT / "NOTICE").read_text()


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_the_chart_carries_the_same_license_and_notice(name):
    # helm packages only what is inside the chart directory; `make licenses` copies the root files there.
    assert (CHART / name).read_bytes() == (ROOT / name).read_bytes()


def test_the_vendored_operator_chart_keeps_its_own_license_and_notice():
    vendored = CHART / "charts" / "opentelemetry-operator"
    assert b"Apache License" in (vendored / "LICENSE").read_bytes()
    assert b"OpenTelemetry Authors" in (vendored / "NOTICE").read_bytes()


def test_the_chart_names_its_license_for_artifact_hub():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["annotations"]["artifacthub.io/license"] == "Apache-2.0"


def test_the_package_ignore_file_does_not_leave_the_license_out():
    ignored = (CHART / ".helmignore").read_text().split()
    assert not [pattern for pattern in ignored if pattern.upper().startswith(("LICENSE", "NOTICE"))]

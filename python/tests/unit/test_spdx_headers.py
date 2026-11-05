# SPDX-License-Identifier: Apache-2.0
"""Every source file of ours carries `SPDX-License-Identifier: Apache-2.0` (scripts/check_spdx.py)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_checker():
    spec = importlib.util.spec_from_file_location("check_spdx", ROOT / "scripts" / "check_spdx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spdx = load_checker()


def test_every_source_file_of_ours_carries_the_spdx_header():
    assert spdx.missing(ROOT) == []


@pytest.mark.parametrize(
    "path",
    [
        "chart/impact-ai/charts/opentelemetry-operator/templates/deployment.yaml",
        "chart/impact-ai/crds/instrumentations.opentelemetry.io.yaml",
        "chart/impact-ai/values.schema.json",
        "python/requirements.lock",
        "testing/apps/requirements-agents.txt",
        "reporter/go.mod",
        ".github/workflows/ci.yml",
        "LICENSE",
        "NOTICE",
        "chart/impact-ai/LICENSE",
        "README.md",
    ],
)
def test_vendored_generated_and_comment_free_files_are_left_alone(path):
    assert spdx.kind_of(path) is None


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("python/src/impact_instrumentation/detect.py", "hash"),
        ("reporter/cmd/reporter/main.go", "slash"),
        ("scripts/e2e.sh", "hash"),
        ("python/Dockerfile", "hash"),
        ("Makefile", "hash"),
        ("mise.toml", "hash"),
        ("chart/impact-ai/values.yaml", "hash"),
        ("chart/impact-ai/templates/reporter.yaml", "template"),
        ("chart/impact-ai/templates/_helpers.tpl", "template"),
        ("chart/impact-ai/templates/NOTES.txt", "template"),
    ],
)
def test_the_header_goes_in_the_comment_syntax_of_the_file(path, kind):
    assert spdx.kind_of(path) == kind


def test_a_header_is_added_after_a_shebang_and_after_a_syntax_directive():
    shell = spdx.with_header("#!/usr/bin/env bash\nset -e\n", "hash")
    assert shell == "#!/usr/bin/env bash\n# SPDX-License-Identifier: Apache-2.0\n\nset -e\n"
    docker = spdx.with_header("# syntax=docker/dockerfile:1\nFROM scratch\n", "hash")
    assert docker.startswith("# syntax=docker/dockerfile:1\n# SPDX-License-Identifier: Apache-2.0\n")


def test_a_go_header_is_set_apart_from_the_package_documentation():
    source = spdx.with_header("// Package x does y.\npackage x\n", "slash")
    assert source == "// SPDX-License-Identifier: Apache-2.0\n\n// Package x does y.\npackage x\n"


def test_a_template_header_is_a_template_comment_that_renders_nothing():
    text = spdx.with_header("apiVersion: v1\n", "template")
    assert text == "{{- /* SPDX-License-Identifier: Apache-2.0 */ -}}\napiVersion: v1\n"


def test_adding_a_header_twice_changes_nothing_more():
    once = spdx.with_header("x = 1\n", "hash")
    assert spdx.has_header(once)
    assert not spdx.has_header("x = 1\n")


def test_a_new_file_is_checked_before_it_is_added_but_an_ignored_one_is_not(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "new.py").write_text("print(1)\n")
    (tmp_path / "ignored.py").write_text("print(2)\n")
    assert spdx.missing(tmp_path) == ["new.py"]

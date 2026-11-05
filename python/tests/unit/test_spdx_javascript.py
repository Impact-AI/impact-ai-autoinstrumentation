# SPDX-License-Identifier: Apache-2.0
"""JavaScript is source of ours too: scripts/check_spdx.py knows its comments, and the Node.js package carries the
header."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_checker():
    spec = importlib.util.spec_from_file_location("check_spdx", ROOT / "scripts" / "check_spdx.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spdx = load_checker()


@pytest.mark.parametrize(
    "path",
    [
        "nodejs/src/autoinstrumentation.js",
        "nodejs/src/impact/bootstrap.js",
        "nodejs/tools/assemble_payload.mjs",
        "nodejs/tests/unit/helpers.js",
        "testing/apps/nodejs/src/openai.cjs",
        "testing/apps/nodejs/src/openai.mjs",
    ],
)
def test_javascript_files_carry_the_header_in_slash_comments(path):
    assert spdx.kind_of(path) == "slash"


def test_installed_packages_and_lock_files_are_not_ours():
    assert spdx.kind_of("nodejs/tests/node_modules/x/index.js") is None
    assert spdx.kind_of("nodejs/trees/node22/package-lock.json") is None
    assert spdx.kind_of("nodejs/trees/node22/package.json") is None


def test_the_header_of_a_node_script_goes_after_its_shebang():
    text = "#!/usr/bin/env node\nimport fs from 'node:fs';\n"
    assert spdx.with_header(text, "slash", "tool.mjs").splitlines()[:2] == [
        "#!/usr/bin/env node",
        "// SPDX-License-Identifier: Apache-2.0",
    ]


def test_a_javascript_file_without_the_header_is_found_and_given_one():
    text = "'use strict';\nmodule.exports = 1;\n"
    assert not spdx.has_header(text)
    assert spdx.has_header(spdx.with_header(text, "slash", "x.js"))


def test_every_file_of_the_nodejs_package_and_its_fixtures_has_the_header():
    missing = [p for p in spdx.missing(ROOT) if p.startswith(("nodejs/", "testing/apps/nodejs/"))]
    assert missing == []

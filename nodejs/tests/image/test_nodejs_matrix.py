# SPDX-License-Identifier: Apache-2.0

"""The qualification matrix of the Node.js image (AE-20): Node 22, 24 and 26, as CommonJS and as ES modules, with
OpenAI's SDK in versions 4 to 7 and Anthropic's from 0.134, against the mock providers.

Every cell runs a fixture workload with the payload injected and asks: did the workload start, and did one model call
arrive as exactly one span with its content? nodejs/qualification.json holds the answers. This test holds them to it:
when a cell changes (a new version of a library, a different Node) it fails and says to run `make qualify`, which runs
the matrix again and writes the file. ES modules run with IMPACT_NODEJS_ESM=on here, so that the matrix finds what
works; whether the preload uses the answers is the next test's business.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import pytest

from containers import wait_until
from nodejs_support import (
    IMAGE,
    NODES,
    QUALIFICATION,
    app_image,
    ask,
    has_content,
    require_images,
    run,
    settle,
    setup_reports,
    wait_ready,
)

LIBRARIES = {
    "openai-4": ("openai", "openai"),
    "openai-5": ("openai", "openai"),
    "openai-6": ("openai", "openai"),
    "openai-7": ("openai", "openai"),
    "anthropic-0": ("anthropic", "@anthropic-ai/sdk"),
    "anthropic-1": ("anthropic", "@anthropic-ai/sdk"),
}
MODULES = ["cjs", "esm"]
CELLS = [(node, module, variant) for node in NODES for module in MODULES for variant in LIBRARIES]
WRITE_TO = os.environ.get("QUALIFY_DIR")


@functools.cache
def installed(node: str, variant: str) -> str:
    package = LIBRARIES[variant][1]
    out = run(
        "run",
        "--rm",
        "--entrypoint",
        "node",
        app_image(node),
        "-p",
        f"require('/apps/{variant}/node_modules/{package}/package.json').version",
    ).stdout
    return out.strip()


def measure(start_app, mock, node: str, module: str, variant: str) -> dict:
    """One cell: did the workload start, and did one model call arrive as exactly one span with its content?"""
    library, _ = LIBRARIES[variant]
    extension = "mjs" if module == "esm" else "cjs"
    result = {
        "node": int(node),
        "tree": None,
        "module": module,
        "library": library,
        "major": int(installed(node, variant).split(".")[0]),
        "version": installed(node, variant),
        "started": False,
        "traced": False,
    }
    app = start_app(
        node, ["node", f"{library}.{extension}"], directory=f"/apps/{variant}", env={"IMPACT_NODEJS_ESM": "on"}
    )
    try:
        wait_ready(app, timeout=40)
        status, _ = ask(app, library)
        result["started"] = status == 200
        if result["started"]:
            spans = settle(mock, 1, timeout=15)
            reports = setup_reports(mock)
            result["traced"] = len(spans) == 1 and has_content(spans[0]) and bool(reports)
    except AssertionError:
        pass  # the application did not start, or no span came
    # the report is made before the application loads: it says which tree this Node uses, even when the application
    # broke
    try:
        wait_until(lambda: setup_reports(mock), timeout=5, message="the setup report")
        result["tree"] = setup_reports(mock)[0]["attrs"].get("impact.setup.node.tree")
    except AssertionError:
        pass
    return result


def key(cell: dict) -> tuple:
    return (cell["node"], cell["module"], cell["library"], cell["major"])


@pytest.mark.nodejs
@pytest.mark.matrix
@pytest.mark.parametrize(("node", "module", "variant"), CELLS, ids=[f"node{n}-{m}-{v}" for n, m, v in CELLS])
def test_the_cell_is_what_the_qualification_says(start_app, mock, node, module, variant):
    require_images(IMAGE, app_image(node))
    result = measure(start_app, mock, node, module, variant)
    if WRITE_TO:
        Path(WRITE_TO).mkdir(parents=True, exist_ok=True)
        (Path(WRITE_TO) / f"node{node}-{module}-{variant}.json").write_text(json.dumps(result, indent=2) + "\n")
        return
    committed = {key(c): c for c in json.loads(QUALIFICATION.read_text())["cells"]}
    assert key(result) in committed, f"no cell for {key(result)} in nodejs/qualification.json: run `make qualify`"
    assert result == committed[key(result)], (
        f"the cell changed: run `make qualify`\n  now:       {result}\n  committed: {committed[key(result)]}"
    )


@pytest.mark.nodejs
@pytest.mark.matrix
def test_the_qualification_holds_every_cell_of_the_matrix_and_nothing_else():
    table = json.loads(QUALIFICATION.read_text())
    assert table["schema"] == "impact.nodejs.qualification.v1"
    expected = {(int(n), m, LIBRARIES[v][0], int(installed(n, v).split(".")[0])) for n, m, v in CELLS}
    assert {key(c) for c in table["cells"]} == expected
    assert table["cells"] == sorted(table["cells"], key=key), "written sorted, so that a change shows as a small diff"


@pytest.mark.nodejs
@pytest.mark.matrix
def test_commonjs_is_traced_everywhere_the_matrix_ran():
    """AE-20's first claim: an uninstrumented Node.js workload, Node 22.15 or later, CommonJS, OpenAI or Anthropic:
    covered."""
    table = json.loads(QUALIFICATION.read_text())
    failing = [c for c in table["cells"] if c["module"] == "cjs" and not (c["started"] and c["traced"])]
    assert failing == []

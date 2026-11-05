# SPDX-License-Identifier: Apache-2.0

"""ES modules, as the preload treats them with the committed qualification (nothing forced): traced where the matrix
proved it,
and where it did not, left alone, reported untraced with the reason `esm`, and never broken."""

from __future__ import annotations

import time

import pytest

from containers import wait_until
from nodejs_support import (
    ANSWER,
    NODES,
    ROOT,
    app_image,
    ask,
    has_content,
    llm_spans,
    only,
    readable,
    require_images,
    settle,
    setup_reports,
    wait_ready,
)


def run_cell(start_app, mock, node, variant, extension, env=None):
    require_images(app_image(node))
    library = variant.split("-")[0]
    app = start_app(node, ["node", f"{library}.{extension}"], directory=f"/apps/{variant}", env=env)
    wait_ready(app)
    assert ask(app, library) == (200, {"answer": ANSWER}), app.logs()  # the application behaves as without injection
    wait_until(lambda: setup_reports(mock), timeout=30, message=lambda: f"the setup report\n{app.logs()}")
    return app


# Every library on Node 22, and the newest of each on the other Nodes: the matrix (test_nodejs_matrix.py) holds every
# cell, with the loader hook forced; here the preload decides by the committed qualification, as it does for a
# customer.
PROVEN = [("22", v) for v in ("openai-5", "openai-6", "openai-7", "anthropic-0", "anthropic-1")] + [
    (node, v) for node in ("24", "26") for v in ("openai-7", "anthropic-1")
]


@pytest.mark.nodejs
@pytest.mark.parametrize(("node", "variant"), PROVEN, ids=[f"{v}-{n}" for n, v in PROVEN])
def test_an_es_module_is_traced_where_the_matrix_proved_it(start_app, mock, node, variant):
    app = run_cell(start_app, mock, node, variant, "mjs")
    span = only(settle(mock, 1), "model-call span")
    assert has_content(span)
    # nothing of the kit's loader hook reaches the application's output: Node 26 deprecates the call that registers it
    assert "Warning" not in app.logs(), app.logs()
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.untraced"] == []
    assert "impact.setup.skipped" not in report["attrs"]


@pytest.mark.nodejs
@pytest.mark.parametrize("node", NODES)
def test_an_es_module_with_openai_4_starts_and_is_reported_untraced_with_the_reason_esm(start_app, mock, node):
    """The loader hook breaks openai 4's ES module build, so it is not registered where that library is: no span, no
    harm."""
    run_cell(start_app, mock, node, "openai-4", "mjs")
    time.sleep(1.5)
    assert llm_spans(mock) == []
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.state"] == "active"
    assert report["attrs"]["impact.setup.untraced"] == ["openai 4.104.0"]
    assert report["attrs"]["impact.setup.skipped"] == f"openai: esm: not proven for Node {node} (4.104.0)"
    assert "openai" not in report["attrs"]["impact.setup.instrumentations"].split(",")


@pytest.mark.nodejs
def test_commonjs_with_openai_4_is_traced(start_app, mock):
    run_cell(start_app, mock, "22", "openai-4", "cjs")
    span = only(settle(mock, 1), "model-call span")
    assert has_content(span)


@pytest.mark.nodejs
def test_the_workload_can_keep_the_package_away_from_es_modules(start_app, mock):
    run_cell(start_app, mock, "22", "openai-6", "mjs", env={"IMPACT_NODEJS_ESM": "off"})
    time.sleep(1.5)
    assert llm_spans(mock) == []
    report = only(setup_reports(mock), "setup report")
    assert report["attrs"]["impact.setup.untraced"] == ["openai 6.49.0"]
    assert report["attrs"]["impact.setup.skipped"].startswith("openai: esm: not proven for Node 22")


@pytest.mark.nodejs
def test_an_entry_point_with_a_package_type_of_module_is_an_es_module_too(start_app, mock, tmp_path):
    """A `.js` file next to a package.json that says "type": "module": the way most ES module applications are
    written."""
    source = tmp_path / "app.js"
    source.write_text((ROOT / "testing" / "apps" / "nodejs" / "src" / "openai.mjs").read_text())
    manifest = tmp_path / "package.json"
    manifest.write_text('{"name":"esm-app","type":"module"}')
    readable(tmp_path)
    app = start_app(
        "22",
        ["node", "app.js"],
        directory="/apps/openai-6",
        mounts={str(source): "/apps/openai-6/app.js:ro", str(manifest): "/apps/openai-6/package.json:ro"},
    )
    wait_ready(app)
    assert ask(app, "openai") == (200, {"answer": ANSWER})
    span = only(settle(mock, 1), "model-call span")
    assert has_content(span)

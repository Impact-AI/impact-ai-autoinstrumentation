# SPDX-License-Identifier: Apache-2.0
"""scripts/qualify_nodejs.py: nodejs/qualification.json from the cells the matrix measured, sorted so that a change is
small."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_nodejs  # noqa: E402


def cell(node, module, library, major, *, traced=True, started=True, tree=None) -> dict:
    return {
        "node": node,
        "tree": tree or "node22",
        "module": module,
        "library": library,
        "major": major,
        "version": f"{major}.0.0",
        "started": started,
        "traced": traced,
    }


def write(directory: Path, cells: list[dict]) -> Path:
    directory.mkdir(exist_ok=True)
    for index, item in enumerate(cells):
        (directory / f"cell-{index}.json").write_text(json.dumps(item))
    return directory


def test_the_cells_are_written_sorted_with_the_schema(tmp_path):
    directory = write(
        tmp_path / "cells",
        [
            cell(24, "esm", "openai", 6),
            cell(22, "cjs", "anthropic", 1),
            cell(22, "cjs", "openai", 5),
            cell(22, "esm", "openai", 5),
        ],
    )
    out = tmp_path / "qualification.json"
    assert qualify_nodejs.main([str(directory), str(out)]) == 0
    table = json.loads(out.read_text())
    assert table["schema"] == "impact.nodejs.qualification.v1"
    assert [(c["node"], c["module"], c["library"], c["major"]) for c in table["cells"]] == [
        (22, "cjs", "anthropic", 1),
        (22, "cjs", "openai", 5),
        (22, "esm", "openai", 5),
        (24, "esm", "openai", 6),
    ]
    assert out.read_text().endswith("}\n")


def test_no_cells_is_an_error_that_says_what_to_run(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="make qualify"):
        qualify_nodejs.assemble(tmp_path / "empty")


def test_two_cells_for_one_place_in_the_matrix_are_refused(tmp_path):
    directory = write(tmp_path / "cells", [cell(22, "cjs", "openai", 5), cell(22, "cjs", "openai", 5, traced=False)])
    with pytest.raises(SystemExit, match="two cells"):
        qualify_nodejs.assemble(directory)


def test_a_cell_with_other_fields_than_the_schema_is_refused(tmp_path):
    odd = cell(22, "cjs", "openai", 5)
    del odd["tree"]
    with pytest.raises(SystemExit, match="fields"):
        qualify_nodejs.assemble(write(tmp_path / "cells", [odd]))


def test_the_committed_table_is_in_the_form_the_script_writes():
    table = json.loads((ROOT / "nodejs" / "qualification.json").read_text())
    assert table["schema"] == "impact.nodejs.qualification.v1"
    assert (ROOT / "nodejs" / "qualification.json").read_text() == json.dumps(table, indent=2) + "\n"
    assert all(set(c) == qualify_nodejs.FIELDS for c in table["cells"])
    assert table["cells"] == sorted(table["cells"], key=qualify_nodejs.key)

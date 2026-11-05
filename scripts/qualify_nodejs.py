#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Write nodejs/qualification.json from the cells the Node.js matrix measured.

    python3 scripts/qualify_nodejs.py build/qualification nodejs/qualification.json

`make qualify` runs the matrix (nodejs/tests/image/test_nodejs_matrix.py) with QUALIFY_DIR set, which makes every cell
write its answer to a file of its own there instead of comparing it with the committed table, and then runs this. The
file is sorted, so that a change of one cell shows as a small diff. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCHEMA = "impact.nodejs.qualification.v1"
FIELDS = {"node", "tree", "module", "library", "major", "version", "started", "traced"}


def key(cell: dict) -> tuple:
    return (cell["node"], cell["module"], cell["library"], cell["major"])


def assemble(directory: Path) -> dict:
    cells = [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]
    if not cells:
        raise SystemExit(f"no cells in {directory}: run the matrix first (make qualify)")
    for cell in cells:
        if set(cell) != FIELDS:
            raise SystemExit(f"a cell with the fields {sorted(cell)}, expected {sorted(FIELDS)}: {cell}")
    keys = [key(cell) for cell in cells]
    if len(keys) != len(set(keys)):
        raise SystemExit("two cells for the same Node, module system, library and major version")
    return {"schema": SCHEMA, "cells": sorted(cells, key=key)}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    table = assemble(Path(argv[0]))
    Path(argv[1]).write_text(json.dumps(table, indent=2) + "\n")
    traced = sum(1 for cell in table["cells"] if cell["traced"])
    print(f"{argv[1]}: {len(table['cells'])} cells, {traced} traced", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

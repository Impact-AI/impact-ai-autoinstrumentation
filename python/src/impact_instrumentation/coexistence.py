# SPDX-License-Identifier: Apache-2.0

"""The coexistence list: the tools the package is proven to run alongside, and what the setup report says of the tool.

The list ships with the package as coexistence.json beside this module, in the form of the image's qualification: one
cell per tool, mode, Python and version series of the tool (its major version, or for a 0.x version its minor), with the
version the proof ran and what it found. An `alongside` cell passes when the package started beside the tool
(`started`), traced the workload (`traced`), and the tool's own backend recorded the same as without the package once
generated ids and times are masked (`same`); `proof` names the test that showed it.

A workload whose tracer the package found (detect.find_foreign) is run alongside only when every finding shows one tool
and a passing alongside cell of that tool matches the workload's Python and the tool's version series exactly. Any other
workload keeps the stand-down: a tool joins the list, and a version series joins a tool, only with a passing proof.

Read from JSON only when a tool was found, so a workload without one pays nothing for it. A list that cannot be read
proves nothing: the package stands down as it would without it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from impact_instrumentation import detect, qualification

SCHEMA = "impact.coexistence.v1"
FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "coexistence.json")
# The coexistence modes by the names impact.ai's service uses; only alongside changes what the package does at start.
MODES = ("alongside", "copy", "relay", "capture", "import")


def load(path: str | None = None) -> Mapping:
    """The shipped list; ValueError for a file that is not one, OSError when it cannot be read."""
    import json

    with open(path or FILE, encoding="utf-8") as handle:
        table = json.load(handle)
    if not isinstance(table, Mapping) or table.get("schema") != SCHEMA or not isinstance(table.get("cells"), list):
        raise ValueError(f"not a {SCHEMA}")
    return table


def passes(cell: Mapping) -> bool:
    """An alongside cell that proves its tool: started, traced, and the tool's backend recorded the same."""
    return (
        cell.get("mode") == "alongside"
        and cell.get("started") is True
        and cell.get("traced") is True
        and cell.get("same") is True
    )


def proven(table: Mapping, *, tool: str, version: str | None, python: str) -> bool:
    """Whether a passing alongside cell proves `tool` at `version` on `python`."""
    series = qualification.series(version)
    if series is None:
        return False
    return any(
        isinstance(cell, Mapping)
        and cell.get("tool") == tool
        and cell.get("python") == python
        and str(cell.get("major")) == series
        and passes(cell)
        for cell in table.get("cells", [])
    )


def alongside(
    findings: Sequence[detect.Finding],
    tool: detect.Tool | None,
    *,
    table: Mapping | None = None,
    python: str | None = None,
) -> bool:
    """Whether the package starts beside the tracer it found instead of standing down."""
    if not findings or tool is None or tool.version is None:
        return False
    if any(detect.finding_tool(finding) != tool.id for finding in findings):
        return False
    if table is None:
        try:
            table = load()
        except Exception:  # noqa: BLE001 - a list that cannot be read proves nothing
            return False
    return proven(table, tool=tool.id, version=tool.version, python=python or qualification.python_version())


def attributes(tool: detect.Tool | None, started: bool) -> dict[str, str | bool]:
    """The setup report's attributes for the tool beside the package: its id, version and exporter kind when known, and
    whether the package started alongside it. Nothing when no tool was named."""
    if tool is None:
        return {}
    out: dict[str, str | bool] = {"impact.setup.tool": tool.id}
    if tool.version:
        out["impact.setup.tool.version"] = tool.version
    if tool.exporter:
        out["impact.setup.exporter"] = tool.exporter
    out["impact.setup.alongside"] = started
    return out

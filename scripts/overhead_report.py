#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Make the overhead report of a release from what the combinations measured.

    python3 scripts/overhead_report.py build/overhead --version 0.3.0

reads build/overhead/combos/*.json (one record per combination, written by python/tests/overhead) and writes
build/overhead/results.json and build/overhead/OVERHEAD.md. The release stamps both into the chart
(scripts/stamp_chart.py): OVERHEAD.md as a file of the chart and the worst numbers as an annotation of Chart.yaml, so
that every release publishes what it costs. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA = "impact.overhead.v1"
BUDGET = {"cpuMsPerCall": 1.5, "memoryMiB": 40.0, "startSeconds": 2.0}
# The metrics of the overhead definition, by the name of the number the report keeps.
MEASURES = {
    "kit.overhead_per_llm_call_ms": "cpuMsPerCall",
    "kit.overhead_memory_mib": "memoryMiB",
    "kit.overhead_start_s": "startSeconds",
}
FIELDS = {"cpu_ms_per_call": "cpuMsPerCall", "memory_mib": "memoryMiB", "start_s": "startSeconds"}
NAMES = {"python": "Python", "node": "Node.js"}
SERVERS = {"express": "Express"}


def label(record: dict) -> str:
    server = SERVERS.get(record["server"], record["server"])
    calls = f", calling {record['library']}" if record.get("library") else ""
    return f"{NAMES[record['runtime']]} {record['version']}, {server}{calls}"


def load(directory: Path) -> list[dict]:
    records = [json.loads(path.read_text()) for path in sorted((directory / "combos").glob("*.json"))]
    if not records:
        raise SystemExit(f"no records in {directory / 'combos'}: run the overhead measurements first (make overhead)")
    return sorted(
        records,
        key=lambda r: (
            r["runtime"],
            [int(part) for part in r["version"].split(".")],
            r["server"],
            r.get("library") or "",
        ),
    )


def summarize(records: list[dict], version: str) -> dict:
    combos = []
    for record in records:
        numbers = {FIELDS[key]: round(value, 3) for key, value in record["median"].items()}
        combos.append(
            {
                "id": record["id"],
                "workload": label(record),
                **numbers,
                "initCopySeconds": record.get("init_copy_s", 0.0),
                "ok": record["ok"],
                "rerun": len(record["attempts"]) > 1,
            }
        )
    worst = {key: max(c[key] for c in combos) for key in BUDGET}
    first = records[0]
    return {
        "schema": SCHEMA,
        "version": version,
        "budget": BUDGET,
        "method": {
            "rounds": first["rounds"],
            "calls": first["calls"],
            "warmup": first["warmup"],
            "pinned": all(r["machine"]["pinned"] for r in records),
            "pinnedRuntimes": sorted({r["runtime"] for r in records if r["machine"]["pinned"]}),
            "unpinnedRuntimes": sorted({r["runtime"] for r in records if not r["machine"]["pinned"]}),
        },
        "worst": worst,
        "measures": {name: worst[key] for name, key in MEASURES.items()},
        "combos": combos,
        "ok": all(c["ok"] for c in combos),
    }


def render(summary: dict) -> str:
    budget, worst, method = summary["budget"], summary["worst"], summary["method"]

    def row(name: str, cpu: float, memory: float, start: float, init: str = "", note: str = "") -> str:
        return f"| {name} | {cpu:+.2f} ms | {memory:+.1f} MiB | {start:+.2f} s | {init} | {note} |"

    rows = [
        row(
            c["workload"],
            c["cpuMsPerCall"],
            c["memoryMiB"],
            c["startSeconds"],
            f"{c['initCopySeconds']:.2f} s",
            "re-run" if c["rerun"] else "",
        )
        for c in summary["combos"]
    ]
    rows.append(row("Worst of these", worst["cpuMsPerCall"], worst["memoryMiB"], worst["startSeconds"]))
    rows.append(row("Budget", budget["cpuMsPerCall"], budget["memoryMiB"], budget["startSeconds"]))
    pinned, free = (" and ".join(NAMES[r] for r in method[key]) for key in ("pinnedRuntimes", "unpinnedRuntimes"))
    if not free:
        pinned = ", each pinned to a core of its own"
    elif pinned:
        pinned = f", the {pinned} ones each pinned to a core of its own and the {free} ones not"
    node = (
        " A Node.js workload is left on all the cores: V8 runs its garbage collector in threads beside the main one, "
        "and on a single pinned core they starve and the heap grows, which is not what a cluster's Node.js process "
        "does. Its memory is read after both sides have collected all their garbage, because before a collection "
        "the reading says how far V8's heap has grown and not what the instrumentation holds: V8's young generation "
        "grows by doubling and does not shrink on its own, and two identical plain Node.js processes differ by -22 to "
        "+23 MiB before a collection (with ten times the calls the two readings are within 3 MiB of each other). What "
        "the instrumentation costs is what a process holds when nothing waits to be freed. The raw reading, from "
        "before the collection, is kept in the record of each workload (memory_unsettled_mib) beside the one judged. "
        f"The budget is unchanged at +{budget['memoryMiB']:g} MiB. When this method was adopted, five runs in a row "
        "put the median difference at +5.5 to +7.4 MiB on Node.js 22, +6.4 to +9.9 MiB on Node.js 24 and +6.3 to "
        "+10.8 MiB on Node.js 26."
        if "node" in method["unpinnedRuntimes"]
        else ""
    )
    return (
        f"# Overhead of the injected instrumentation, release {summary['version']}\n\n"
        "What a workload costs more with the instrumentation injected than the same workload without it, measured by "
        "this release's build on the images it publishes. The budget, which the build enforces, is "
        f"+{budget['cpuMsPerCall']:g} ms of CPU per model call, +{budget['memoryMiB']:g} MiB of memory and "
        f"+{budget['startSeconds']:g} s to start.\n\n"
        "| Workload | CPU per model call | Memory | Start | Init copy | |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n\n## How it is measured\n\n"
        "Each workload is a small web service that calls a model provider (a mock) on every request. In each of "
        f"{method['rounds']} rounds the service without injection and the same service with the payload injected run "
        f"side by side{pinned}. After {method['warmup']} warm-up calls, {method['calls']} calls are made to each, and "
        "the container's cgroup is read before and after: CPU is the difference of its CPU time per call (the injected "
        "side's export of its spans included), memory is its working set at the end (the Kubernetes definition: the "
        "cgroup's memory less its inactive file cache). Start is the time from starting the container to its first "
        "answer on the health probe. The init container's copy of the payload before it is listed apart: it is not in "
        f"the budget, and pulling an image is not part of it.{node}\n\n"
        "A number in the table is the median of the differences between the two sides of each round. A workload that "
        "is over the budget is measured once more, and the build stops only if it is over again; its row says "
        "re-run.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", default="0.0.0-dev")
    args = parser.parse_args(argv)
    summary = summarize(load(args.directory), args.version)
    (args.directory / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
    (args.directory / "OVERHEAD.md").write_text(render(summary))
    worst = summary["worst"]
    verdict = "within" if summary["ok"] else "OVER"
    print(
        f"overhead of {len(summary['combos'])} combinations: worst +{worst['cpuMsPerCall']:.2f} ms/call, "
        f"+{worst['memoryMiB']:.1f} MiB, +{worst['startSeconds']:.2f} s; {verdict} the budget",
        file=sys.stderr,
    )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

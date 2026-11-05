# SPDX-License-Identifier: Apache-2.0

"""AE-17: for every runtime and server the images claim, an injected workload costs no more than +1.5 ms of CPU per
model call, +40 MiB of memory and +2 s to start than the same workload without injection. Real images, real containers:
`make overhead` for all eleven combinations (and `make release`, which publishes the numbers), `make ci` for a canary
of two, one round each.

OVERHEAD_COMBOS (comma-separated ids) and OVERHEAD_ROUNDS pick what runs; OVERHEAD_DIR is where each combination's
record goes (scripts/overhead_report.py makes the report of them).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import gate
import harness
from combos import all_combos

ROUNDS = int(os.environ.get("OVERHEAD_ROUNDS", "3"))
SELECTED = [name for name in os.environ.get("OVERHEAD_COMBOS", "").split(",") if name]
DIRECTORY = Path(os.environ.get("OVERHEAD_DIR", "build/overhead"))
COMBOS = [c for c in all_combos() if not SELECTED or c.id in SELECTED]


@pytest.mark.overhead
@pytest.mark.parametrize("combo", COMBOS, ids=[c.id for c in COMBOS])
def test_the_injected_workload_is_within_the_budget(combo):
    for image in harness.Setup(combo).images():
        if not harness.python_env.image_exists(image):
            pytest.fail(f"missing image {image}: run `make overhead` (or `make images`)", pytrace=False)
    attempts, records, rounds_of, machine = [], [], [], {}
    while True:
        rounds, machine = harness.measure(combo, ROUNDS)
        verdict = gate.judge(rounds)
        attempts.append(verdict)
        rounds_of.append(rounds)
        records.append(gate.as_record(rounds, verdict))
        if not gate.needs_rerun(attempts):
            break
    decided = gate.final(attempts)
    # the init container's copy, of the attempt that decided: published beside the start time, not judged
    decided_rounds = rounds_of[attempts.index(decided)]
    decided_init = round(gate.median([r.injected.init_s for r in decided_rounds]), 3)
    DIRECTORY.joinpath("combos").mkdir(parents=True, exist_ok=True)
    DIRECTORY.joinpath("combos", f"{combo.id}.json").write_text(
        json.dumps(
            {
                "id": combo.id,
                "runtime": combo.runtime,
                "version": combo.version,
                "server": combo.server,
                "rounds": ROUNDS,
                "calls": harness.CALLS,
                "warmup": harness.WARMUP,
                "machine": machine,
                "attempts": records,
                "median": decided.median,
                "init_copy_s": decided_init,
                "ok": decided.ok,
            },
            indent=2,
        )
        + "\n"
    )
    assert decided.ok, f"{combo.id}: {decided.describe()}" + (" (after a re-run)" if len(attempts) > 1 else "")

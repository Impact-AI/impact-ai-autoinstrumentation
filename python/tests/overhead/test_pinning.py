# SPDX-License-Identifier: Apache-2.0

"""Which workloads the overhead harness pins to a core of their own, and why a Node.js one is not.

Python is pinned to one core per side, so that the two sides of a round do not share one and the CPU time per call is
quiet. A Node.js process on one pinned core is not the Node.js process of a cluster: V8 runs its garbage collector in
threads beside the main one, they starve on a single core, and the heap grows before they catch up. The memory of the
injected side then differs from the plain side's by whatever the collector happened to be doing when it was read: on
the same machine, with the same images, between 14 and 49 MiB with one core each, and between -12 and +22 with the
cores free, where the collector keeps up. Node.js is measured with the cores free.
"""

from __future__ import annotations

import pytest

import harness
from combos import all_combos

BY_ID = {c.id: c for c in all_combos()}


@pytest.fixture
def cores(monkeypatch):
    monkeypatch.setattr(harness, "pick_cpus", lambda: (2, 9))


@pytest.mark.parametrize("combo_id", ["node22-express", "node24-express", "node26-express"])
def test_a_node_workload_is_not_pinned_to_a_single_core_even_where_the_engine_could(cores, combo_id):
    assert harness.cpus_for(BY_ID[combo_id], pinning=True) is None


@pytest.mark.parametrize("combo_id", [c.id for c in all_combos() if c.runtime == "python"])
def test_a_python_workload_gets_a_core_of_its_own_where_the_engine_lets_it(cores, combo_id):
    assert harness.cpus_for(BY_ID[combo_id], pinning=True) == (2, 9)


@pytest.mark.parametrize("combo_id", ["python3.12-uvicorn", "node22-express"])
def test_nothing_is_pinned_where_the_engine_does_not_let_it(cores, combo_id):
    assert harness.cpus_for(BY_ID[combo_id], pinning=False) is None

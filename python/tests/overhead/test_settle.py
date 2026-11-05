# SPDX-License-Identifier: Apache-2.0

"""How the memory of a Node.js workload is read: after its process has collected its garbage, on both sides alike.

The working set of a V8 heap is mostly what the process has not yet let go of (the young generation alone grows by
doubling and does not shrink on its own), so reading it at an arbitrary instant measures how far that growth has got
and not what the kit costs. The collection is asked for with a signal; the memory is read when it has stopped falling.
"""

from __future__ import annotations

import pytest

import harness
from combos import NODE_EXPRESS, NODE_SETTLE, all_combos

MIB = 1048576


class Fake:
    """Containers whose working set follows a script, one reading per call of read_cgroup, and a clock that only moves
    when the harness sleeps."""

    def __init__(self, monkeypatch, series: dict[str, list[int]]):
        self.series = {name: list(values) for name, values in series.items()}
        self.signals: list[tuple[str, ...]] = []
        self.slept = 0.0
        self.clock = 1000.0
        monkeypatch.setattr(harness, "run", lambda *args, **kwargs: self.signals.append(args))
        monkeypatch.setattr(harness, "read_cgroup", self.read)
        monkeypatch.setattr(harness.time, "sleep", self.sleep)
        monkeypatch.setattr(harness.time, "monotonic", lambda: self.clock)

    def read(self, container) -> tuple[int, int]:
        values = self.series[container.name]
        return 0, values.pop(0) if len(values) > 1 else values[0]

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.clock += seconds


class Named:
    def __init__(self, name: str):
        self.name = name


def test_both_sides_are_signalled_and_the_memory_is_read_when_it_has_stopped_falling(monkeypatch):
    fake = Fake(
        monkeypatch,
        {
            "plain": [130 * MIB, 90 * MIB, 70 * MIB, 64 * MIB, 64 * MIB],
            "injected": [150 * MIB, 100 * MIB, 78 * MIB, 76 * MIB, 76 * MIB],
        },
    )
    settled = harness.settle((Named("plain"), Named("injected")), "USR2")
    assert fake.signals == [("kill", "--signal", "USR2", "plain"), ("kill", "--signal", "USR2", "injected")]
    assert settled == {"plain": 64 * MIB, "injected": 76 * MIB}
    assert fake.slept >= harness.SETTLE_WAIT


def test_a_side_that_is_still_falling_keeps_the_other_waiting(monkeypatch):
    # the plain side is quiet at once; the injected one needs two more readings
    fake = Fake(monkeypatch, {"plain": [64 * MIB], "injected": [90 * MIB, 80 * MIB, 70 * MIB, 70 * MIB]})
    settled = harness.settle((Named("plain"), Named("injected")), "USR2")
    assert settled == {"plain": 64 * MIB, "injected": 70 * MIB}
    assert fake.slept >= harness.SETTLE_WAIT + 3 * harness.SETTLE_STEP


def test_readings_within_a_quarter_of_a_mebibyte_are_the_same_reading(monkeypatch):
    Fake(monkeypatch, {"only": [64 * MIB, 64 * MIB + harness.SETTLE_QUIET]})
    assert harness.settle((Named("only"),), "USR2") == {"only": 64 * MIB + harness.SETTLE_QUIET}


def test_a_memory_that_never_settles_is_an_error_and_not_a_number(monkeypatch):
    Fake(monkeypatch, {"only": [(64 + 2 * i) * MIB for i in range(1000)]})
    with pytest.raises(AssertionError, match="still moving"):
        harness.settle((Named("only"),), "USR2")


def test_every_node_workload_is_settled_the_same_way_and_no_python_one_is():
    node = [c for c in all_combos() if c.runtime == "node"]
    assert node and all(c.settle == NODE_SETTLE and c.command == NODE_EXPRESS for c in node)
    assert NODE_EXPRESS[:4] == ("node", "--expose-gc", "--require", "/src/settle.cjs")
    assert all(c.settle is None for c in all_combos() if c.runtime == "python")

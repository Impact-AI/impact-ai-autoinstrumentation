# SPDX-License-Identifier: Apache-2.0

"""The overhead gate (AE-17): what the budget is, and how the measurements are held to it.

A round starts the workload without injection and the same workload with the payload injected, side by side, and
measures both. The overhead of a round is the difference between its two sides, per metric, so that whatever the
machine is doing at that moment (a neighbour, a frequency step) is in both. A combination of runtime and server is
measured in a few rounds and judged on the median of its paired differences: one noisy round cannot fail it, and one
lucky round cannot save it. A combination that fails is measured once more, from scratch, and fails only if the second
measurement fails too; the report keeps both.

The budget is the definition's: +1.5 ms of CPU per model call, +40 MiB of memory, +2 s to start.

Standard library only.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass

# metric -> (budget, unit). A value above the budget fails; a value at it passes.
GATES = {
    "cpu_ms_per_call": (1.5, "ms"),
    "memory_mib": (40.0, "MiB"),
    "start_s": (2.0, "s"),
}
METRICS = tuple(GATES)


@dataclass(frozen=True)
class Side:
    """What one side of a round measured."""

    cpu_ms_per_call: float  # CPU time of the container's cgroup over the measured calls, per call
    memory_mib: float  # working set after the calls (memory.current less inactive file cache), settled: harness.settle
    start_s: float  # from starting the workload to its first answer on the probe
    init_s: float = 0.0  # the init container's copy of the payload, before the workload starts: published, not judged
    memory_unsettled_mib: float | None = None  # the reading before the garbage was collected: published, not judged


@dataclass(frozen=True)
class Round:
    plain: Side
    injected: Side

    def difference(self, metric: str) -> float:
        return getattr(self.injected, metric) - getattr(self.plain, metric)


@dataclass(frozen=True)
class Verdict:
    """One measurement of one combination: per metric the paired differences, their median and whether it is within
    budget."""

    differences: dict[str, list[float]]
    median: dict[str, float]
    ok_by_metric: dict[str, bool]

    @property
    def ok(self) -> bool:
        return all(self.ok_by_metric.values())

    def failing(self) -> list[str]:
        return [metric for metric, ok in self.ok_by_metric.items() if not ok]

    def describe(self) -> str:
        parts = []
        for metric, (budget, unit) in GATES.items():
            mark = "ok" if self.ok_by_metric[metric] else "OVER"
            parts.append(f"{metric} {self.median[metric]:+.2f} {unit} (budget +{budget:g}, {mark})")
        return "; ".join(parts)


def paired_differences(rounds: Sequence[Round]) -> dict[str, list[float]]:
    return {metric: [r.difference(metric) for r in rounds] for metric in METRICS}


def median(values: Sequence[float]) -> float:
    return statistics.median(values)


def judge(rounds: Sequence[Round]) -> Verdict:
    """The verdict of one measurement: the median of the paired differences against each budget."""
    if not rounds:
        raise ValueError("a measurement has at least one round")
    differences = paired_differences(rounds)
    medians = {metric: median(values) for metric, values in differences.items()}
    return Verdict(
        differences=differences,
        median=medians,
        ok_by_metric={metric: medians[metric] <= GATES[metric][0] for metric in METRICS},
    )


def final(attempts: Sequence[Verdict]) -> Verdict:
    """The verdict of a combination from its attempts: the first, unless it failed; then the re-run, which replaces
    it."""
    if not attempts:
        raise ValueError("a combination has at least one attempt")
    if attempts[0].ok or len(attempts) == 1:
        return attempts[0]
    return attempts[1]


def needs_rerun(attempts: Sequence[Verdict]) -> bool:
    return len(attempts) == 1 and not attempts[0].ok


def as_record(rounds: Sequence[Round], verdict: Verdict) -> dict:
    """The attempt as the report keeps it: every round, and what was concluded."""
    return {
        "rounds": [{"plain": asdict(r.plain), "injected": asdict(r.injected)} for r in rounds],
        "differences": verdict.differences,
        "median": verdict.median,
        "ok": verdict.ok,
    }

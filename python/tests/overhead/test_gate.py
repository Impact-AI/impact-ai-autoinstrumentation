# SPDX-License-Identifier: Apache-2.0

"""The overhead gate, without containers: the budget, the paired median, the re-run, and what the report makes of
them."""

from __future__ import annotations

import pytest

from gate import GATES, Round, Side, as_record, final, judge, median, needs_rerun, paired_differences


def side(cpu=3.0, memory=60.0, start=1.0) -> Side:
    return Side(cpu_ms_per_call=cpu, memory_mib=memory, start_s=start)


def rnd(extra_cpu=0.5, extra_memory=10.0, extra_start=0.5, *, base_cpu=3.0, base_memory=60.0, base_start=1.0) -> Round:
    """A round in which the injected side costs the given extra over the plain one."""
    return Round(
        plain=side(base_cpu, base_memory, base_start),
        injected=side(base_cpu + extra_cpu, base_memory + extra_memory, base_start + extra_start),
    )


def test_the_budget_is_the_definitions():
    assert GATES == {"cpu_ms_per_call": (1.5, "ms"), "memory_mib": (40.0, "MiB"), "start_s": (2.0, "s")}


def test_a_workload_within_the_budget_passes():
    verdict = judge([rnd(), rnd(), rnd()])
    assert verdict.ok and verdict.failing() == []
    assert verdict.median == {"cpu_ms_per_call": 0.5, "memory_mib": 10.0, "start_s": 0.5}


@pytest.mark.parametrize(
    ("kwargs", "metric"),
    [
        ({"extra_cpu": 1.51}, "cpu_ms_per_call"),
        ({"extra_memory": 40.01}, "memory_mib"),
        ({"extra_start": 2.01}, "start_s"),
    ],
)
def test_each_metric_fails_above_its_budget(kwargs, metric):
    verdict = judge([rnd(**kwargs)] * 3)
    assert not verdict.ok
    assert verdict.failing() == [metric]
    assert "OVER" in verdict.describe()


@pytest.mark.parametrize("kwargs", [{"extra_cpu": 1.5}, {"extra_memory": 40.0}, {"extra_start": 2.0}])
def test_a_value_at_the_budget_passes(kwargs):
    assert judge([rnd(**kwargs)]).ok


def test_it_is_the_difference_within_a_round_that_counts_not_the_difference_of_the_medians():
    # the machine got slower in the second round: both sides did. The paired differences are all 1.0 ms.
    rounds = [rnd(extra_cpu=1.0, base_cpu=3.0), rnd(extra_cpu=1.0, base_cpu=9.0), rnd(extra_cpu=1.0, base_cpu=30.0)]
    assert paired_differences(rounds)["cpu_ms_per_call"] == [1.0, 1.0, 1.0]
    assert judge(rounds).ok


def test_one_noisy_round_cannot_fail_a_combination_and_one_lucky_round_cannot_save_it():
    assert judge([rnd(extra_cpu=0.5), rnd(extra_cpu=9.0), rnd(extra_cpu=0.6)]).ok  # the median is 0.6
    assert not judge([rnd(extra_cpu=0.1), rnd(extra_cpu=1.9), rnd(extra_cpu=2.4)]).ok  # the median is 1.9


def test_two_noisy_rounds_of_three_do_fail():
    assert not judge([rnd(extra_cpu=0.5), rnd(extra_cpu=9.0), rnd(extra_cpu=8.0)]).ok


def test_a_single_round_is_its_own_median():
    assert judge([rnd(extra_cpu=1.2)]).median["cpu_ms_per_call"] == pytest.approx(1.2)


def test_an_even_number_of_rounds_has_the_middle_of_the_two_in_the_middle():
    assert median([1.0, 2.0, 3.0, 10.0]) == 2.5


def test_a_measurement_needs_rounds():
    with pytest.raises(ValueError, match="at least one round"):
        judge([])


def test_a_passing_first_attempt_is_final_and_is_not_run_again():
    first = judge([rnd()])
    assert final([first]) is first and not needs_rerun([first])


def test_a_failing_first_attempt_is_run_again_once():
    first = judge([rnd(extra_cpu=3.0)])
    assert needs_rerun([first])
    second = judge([rnd(extra_cpu=0.4)])
    assert not needs_rerun([first, second])
    assert final([first, second]) is second  # the re-run replaces the first measurement: it passes
    third = judge([rnd(extra_cpu=3.5)])
    assert final([first, third]) is third and not final([first, third]).ok  # and fails if it fails too


def test_a_combination_has_attempts():
    with pytest.raises(ValueError, match="at least one attempt"):
        final([])


def test_the_attempt_is_kept_with_every_round():
    rounds = [rnd(extra_cpu=0.2), rnd(extra_cpu=0.3)]
    record = as_record(rounds, judge(rounds))
    assert len(record["rounds"]) == 2 and record["ok"] is True
    assert record["rounds"][0]["injected"]["cpu_ms_per_call"] == pytest.approx(3.2)
    assert record["median"]["cpu_ms_per_call"] == pytest.approx(0.25)
    assert set(record) == {"rounds", "differences", "median", "ok"}


def test_the_init_containers_copy_is_published_and_never_judged():
    rounds = [
        Round(plain=side(), injected=Side(cpu_ms_per_call=3.5, memory_mib=70.0, start_s=1.5, init_s=9.0))
        for _ in range(3)
    ]
    assert judge(rounds).ok  # nine seconds of copying are not part of the start the budget is about
    assert as_record(rounds, judge(rounds))["rounds"][0]["injected"]["init_s"] == 9.0
    assert as_record(rounds, judge(rounds))["rounds"][0]["plain"]["init_s"] == 0.0


def test_the_reading_before_the_garbage_was_collected_is_published_and_never_judged():
    rounds = [
        Round(
            plain=Side(cpu_ms_per_call=3.0, memory_mib=92.0, start_s=1.0, memory_unsettled_mib=100.0),
            injected=Side(cpu_ms_per_call=3.5, memory_mib=99.0, start_s=1.5, memory_unsettled_mib=150.0),
        )
        for _ in range(3)
    ]
    verdict = judge(rounds)
    assert verdict.median["memory_mib"] == pytest.approx(7.0)
    assert verdict.ok  # the unsettled readings are 50 MiB apart; the budget is held to the settled ones
    record = as_record(rounds, verdict)
    assert record["rounds"][0]["plain"]["memory_unsettled_mib"] == 100.0
    assert record["rounds"][0]["injected"]["memory_unsettled_mib"] == 150.0
    assert as_record([rnd()], judge([rnd()]))["rounds"][0]["injected"]["memory_unsettled_mib"] is None

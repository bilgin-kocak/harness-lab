"""Paired statistics: bootstrap intervals, sign test, pairing, verdicts."""

import pytest

from harnesslab.experiments.aggregate import RunSample, compare_variants
from harnesslab.experiments.stats import (
    bootstrap_mean_diff,
    exact_sign_test,
    paired_comparison,
    paired_tasks,
)


def _s(task, variant, passed, rep=0, cost=None, calls=None):
    return RunSample(
        run_id=f"{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        verified_pass=passed,
        reported_cost_usd=cost,
        llm_calls=calls,
    )


def test_bootstrap_known_effect_no_effect_and_determinism():
    strong = bootstrap_mean_diff([1.0] * 6)
    assert (strong.estimate, strong.low, strong.high, strong.p_positive) == (1.0, 1.0, 1.0, 1.0)
    none = bootstrap_mean_diff([0.0] * 6)
    assert none.low == 0.0 and none.high == 0.0 and none.p_positive == 0.0
    mixed = bootstrap_mean_diff([1, 0, 1, 0, 1, 0, 1, 0], seed=3)
    assert mixed.low < mixed.estimate < mixed.high and mixed.low > 0
    assert mixed == bootstrap_mean_diff([1, 0, 1, 0, 1, 0, 1, 0], seed=3)
    noisy = bootstrap_mean_diff([1, -1, 1, -1, 0, 0])
    assert noisy.low < 0 < noisy.high
    assert bootstrap_mean_diff([]) is None


def test_exact_sign_test_values():
    assert exact_sign_test(3, 0) == pytest.approx(0.25)
    assert exact_sign_test(6, 0) == pytest.approx(0.03125)
    assert exact_sign_test(5, 5) == 1.0
    assert exact_sign_test(0, 0) is None


def test_pairing_averages_repetitions_and_drops_unpaired_tasks():
    samples = [
        _s("t1", "A", False),
        _s("t1", "A", True, rep=1),  # A: 50% on t1
        _s("t1", "B", True),
        _s("t1", "B", True, rep=1),
        _s("t2", "A", True),
        _s("t2", "B", None),  # B has no verified run on t2: unpaired
        _s("t3", "B", True),  # A never ran t3
    ]
    tasks = paired_tasks(samples, "A", "B")
    assert [t.task_key for t in tasks] == ["t1"]
    assert tasks[0].a_rate == 0.5 and tasks[0].b_rate == 1.0


def test_verdicts_need_enough_tasks():
    six = [x for i in range(6) for x in (_s(f"t{i}", "A", False), _s(f"t{i}", "B", True))]
    better = paired_comparison(six, "A", "B")
    assert (
        better.verdict == "better"
        and better.wins == 6
        and better.sign_test_p == pytest.approx(0.03125)
    )
    worse = paired_comparison(six, "B", "A")
    assert worse.verdict == "worse"
    three = six[:6]
    assert paired_comparison(three, "A", "B").verdict == "not enough tasks"
    assert paired_comparison(three, "A", "B", min_tasks=3).verdict == "better"
    same = [x for i in range(6) for x in (_s(f"t{i}", "A", True), _s(f"t{i}", "B", True))]
    assert paired_comparison(same, "A", "B").verdict == "no evidence"


def test_cost_and_llm_call_differences():
    samples = [
        x
        for i in range(5)
        for x in (
            _s(f"t{i}", "A", True, cost=0.10, calls=4),
            _s(f"t{i}", "B", True, cost=0.04, calls=2),
        )
    ]
    result = paired_comparison(samples, "A", "B")
    assert result.cost_kind == "reported_cost_usd"
    assert result.cost_diff.estimate == pytest.approx(-0.06) and result.cost_diff.high < 0
    assert result.llm_calls_diff.estimate == pytest.approx(-2.0)
    assert result.verdict == "no evidence"  # same pass rate, cheaper: the cost interval says so


def test_compare_variants_carries_paired_evidence():
    samples = [x for i in range(5) for x in (_s(f"t{i}", "A", False), _s(f"t{i}", "B", True))]
    comparison = compare_variants(samples, "A", "B", [f"t{i}" for i in range(5)])
    assert comparison.paired is not None and comparison.paired.verdict == "better"
    assert comparison.paired.n_tasks == 5

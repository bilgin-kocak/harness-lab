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


def _m(task, variant, passed, score=None, ratio=None, rep=0):
    return RunSample(
        run_id=f"{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        verified_pass=passed,
        verified_score=score,
        improve_ratio=ratio,
    )


def test_score_verdict_where_pass_rate_has_no_evidence():
    # Both sides pass every task; B's partial score is higher on all six.
    samples = [
        x
        for i in range(6)
        for x in (_m(f"t{i}", "A", True, score=0.5), _m(f"t{i}", "B", True, score=0.8))
    ]
    by_rate = paired_comparison(samples, "A", "B")
    assert by_rate.verdict == "no evidence" and by_rate.metric == "pass_rate"
    assert by_rate.score_diff.estimate == pytest.approx(0.3)
    by_score = paired_comparison(samples, "A", "B", metric="score")
    assert by_score.verdict == "better" and by_score.metric == "score"
    assert by_score.metric_diff == by_score.score_diff
    assert by_score.pass_rate_diff == by_rate.pass_rate_diff  # still reported alongside
    assert by_score.tasks[0].a_score == 0.5 and by_score.tasks[0].b_score == 0.8
    # The sign test runs on the score differences too: six wins, no losses.
    assert (by_score.wins, by_score.losses, by_score.ties) == (6, 0, 0)
    assert by_score.sign_test_p == pytest.approx(0.03125)
    assert (by_rate.wins, by_rate.losses, by_rate.ties) == (0, 0, 6)
    assert by_rate.sign_test_p is None


def test_score_averages_repetitions_and_ignores_runs_without_score():
    samples = [
        _m("t1", "A", True, score=0.2),
        _m("t1", "A", True, score=0.4, rep=1),
        _m("t1", "A", True, score=None, rep=2),
        _m("t1", "B", False),  # no score at all on B
    ]
    (task,) = paired_tasks(samples, "A", "B")
    assert task.a_score == pytest.approx(0.3) and task.b_score is None


def test_min_tasks_counts_tasks_that_have_the_metric():
    samples = []
    for i in range(6):
        has = i < 4  # only four tasks have a score on both sides
        samples.append(_m(f"t{i}", "A", False, score=0.0 if has else None))
        samples.append(_m(f"t{i}", "B", True, score=1.0 if has else None))
    by_score = paired_comparison(samples, "A", "B", metric="score")
    assert by_score.n_tasks == 6 and by_score.n_metric_tasks == 4
    assert by_score.verdict == "not enough tasks" and not by_score.enough_tasks
    assert (by_score.wins, by_score.losses, by_score.ties) == (4, 0, 0)
    assert paired_comparison(samples, "A", "B", metric="score", min_tasks=4).verdict == "better"
    by_rate = paired_comparison(samples, "A", "B")
    assert by_rate.n_metric_tasks == 6 and by_rate.verdict == "better"


def test_improve_ratio_verdict_uses_only_tasks_with_both_ratios():
    samples = []
    for i in range(7):
        # A has no ratio on two tasks (its final state failed there); B improves everywhere.
        samples.append(_m(f"t{i}", "A", True, ratio=1.2 if i < 5 else None))
        samples.append(_m(f"t{i}", "B", True, ratio=2.0))
    result = paired_comparison(samples, "A", "B", metric="improve_ratio")
    assert result.n_metric_tasks == 5 and result.verdict == "better"
    assert result.metric_diff == result.improve_ratio_diff
    assert result.metric_diff.estimate == pytest.approx(0.8)
    assert (result.wins, result.losses, result.ties) == (5, 0, 0)
    assert paired_comparison(samples, "A", "B", metric="improve_ratio", min_tasks=6).verdict == (
        "not enough tasks"
    )


def test_default_metric_is_pass_rate_and_unchanged():
    # B passes more often but scores lower: the default verdict is about pass rate only.
    samples = [
        x
        for i in range(6)
        for x in (_m(f"t{i}", "A", False, score=0.9), _m(f"t{i}", "B", True, score=0.1))
    ]
    default = paired_comparison(samples, "A", "B")
    assert default == paired_comparison(samples, "A", "B", metric="pass_rate")
    assert default.verdict == "better" and default.wins == 6
    assert default.n_metric_tasks == default.n_tasks == 6
    assert default.metric_diff == default.pass_rate_diff
    assert paired_comparison(samples, "A", "B", metric="score").verdict == "worse"


def test_unknown_metric_raises():
    with pytest.raises(ValueError, match="unknown verdict metric"):
        paired_comparison([_m("t1", "A", True), _m("t1", "B", True)], "A", "B", metric="cost")


def test_compare_variants_passes_the_metric_through():
    samples = [
        x
        for i in range(5)
        for x in (_m(f"t{i}", "A", True, score=0.1), _m(f"t{i}", "B", True, score=0.9))
    ]
    tasks = [f"t{i}" for i in range(5)]
    assert compare_variants(samples, "A", "B", tasks).paired.verdict == "no evidence"
    scored = compare_variants(samples, "A", "B", tasks, metric="score")
    assert scored.paired.metric == "score" and scored.paired.verdict == "better"


def test_float_noise_is_a_tie_not_a_win():
    # Means that are equal on paper differ in the last bits: that must not become a verdict.
    samples = []
    for task in ("t0", "t1", "t2"):
        samples += [_m(task, "A", True, score=v, rep=i) for i, v in enumerate((0.1, 0.2, 0.3))]
        samples += [_m(task, "B", True, score=0.2, rep=i) for i in range(2)]
    result = paired_comparison(samples, "A", "B", metric="score", min_tasks=1)
    assert (result.wins, result.losses, result.ties) == (0, 0, 3)
    assert result.verdict == "no evidence"

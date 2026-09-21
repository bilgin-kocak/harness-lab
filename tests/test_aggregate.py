from harnesslab.experiments.aggregate import (
    RunSample,
    aggregate_variants,
    build_matrix,
    compare_variants,
    describe,
)


def _s(task, variant, passed, score=None, rep=0, status="completed", **kw):
    return RunSample(
        run_id=f"r-{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        status=status,
        outcome=("pass" if passed else "fail") if passed is not None else "not_verified",
        verified_pass=passed,
        verified_score=score if score is not None else (None if passed is None else float(passed)),
        **kw,
    )


SAMPLES = [
    _s(
        "t1",
        "A",
        True,
        wall_time_seconds=10,
        input_tokens=100,
        output_tokens=10,
        tool_calls=3,
        files_changed=1,
        reported_cost_usd=0.5,
    ),
    _s(
        "t2",
        "A",
        False,
        wall_time_seconds=30,
        input_tokens=300,
        output_tokens=30,
        tool_calls=9,
        files_changed=0,
        reported_cost_usd=1.5,
    ),
    _s("t3", "A", None, status="unavailable"),
    _s(
        "t1",
        "B",
        True,
        wall_time_seconds=20,
        input_tokens=200,
        output_tokens=20,
        tool_calls=6,
        files_changed=2,
    ),
    _s(
        "t2",
        "B",
        True,
        score=0.5,
        wall_time_seconds=40,
        input_tokens=400,
        output_tokens=40,
        tool_calls=12,
        files_changed=1,
    ),
    _s(
        "t3",
        "B",
        False,
        wall_time_seconds=60,
        input_tokens=600,
        output_tokens=60,
        tool_calls=15,
        files_changed=3,
    ),
]


def test_describe_handles_small_samples():
    assert describe([]).n == 0 and describe([1]).std is None
    d = describe([1, None, 3])
    assert (d.n, d.mean, d.median, d.min, d.max) == (2, 2.0, 2.0, 1.0, 3.0) and round(
        d.std, 4
    ) == 1.4142


def test_variant_aggregates_exclude_unverified_from_success_rate():
    aggs = aggregate_variants(SAMPLES, ["A", "B"])
    a, b = aggs["A"], aggs["B"]
    assert (a.n_total, a.n_valid, a.n_passed, a.n_not_verified, a.n_infra_failures) == (
        3,
        2,
        1,
        1,
        1,
    )
    assert (
        a.success_rate == 0.5
        and a.score.mean == 0.5
        and a.wall_time_seconds.median == 20
        and a.input_tokens.median == 200
    )
    assert a.reported_cost_usd.n == 2 and a.reported_cost_usd.median == 1.0
    assert (
        b.success_rate == 2 / 3
        and round(b.score.mean, 4) == 0.5
        and b.reported_cost_usd.n == 0
        and b.reported_cost_usd.median is None
    )
    assert a.per_task_pass_rate == {"t1": 1.0, "t2": 0.0} and b.per_task_pass_rate["t2"] == 1.0
    assert aggregate_variants([], ["A"])["A"].success_rate is None


def test_matrix_states_and_repetitions():
    reps = SAMPLES + [_s("t1", "A", False, rep=1, wall_time_seconds=11)]
    matrix = build_matrix(reps, ["t1", "t2", "t3"], ["A", "B"])
    assert (
        matrix["t1"]["A"].state == "mixed"
        and matrix["t1"]["A"].n == 2
        and matrix["t1"]["A"].pass_rate == 0.5
    )
    assert (
        matrix["t2"]["A"].state == "fail"
        and matrix["t3"]["A"].state == "error"
        and matrix["t1"]["B"].state == "pass"
    )
    assert (
        matrix["t2"]["B"].mean_score == 0.5
        and build_matrix([], ["t9"], ["A"])["t9"]["A"].state == "empty"
    )


def test_compare_variants_categories_and_deltas():
    cmp = compare_variants(SAMPLES, "A", "B", ["t1", "t2", "t3"])
    assert [t.category for t in cmp.tasks] == ["both_passed", "b_only", "unverified"]
    assert (
        cmp.summary["both_passed"] == 1
        and cmp.summary["b_only"] == 1
        and cmp.summary["unverified"] == 1
        and cmp.summary["a_only"] == 0
    )
    deltas = {d.metric: d for d in cmp.deltas}
    assert deltas["success_rate"].a == 0.5 and round(deltas["success_rate"].delta, 4) == round(
        2 / 3 - 0.5, 4
    )
    assert deltas["input_tokens"].delta == 200 and deltas["input_tokens"].lower_is_better
    assert deltas["reported_cost_usd"].delta is None
    mixed = compare_variants(SAMPLES + [_s("t1", "A", False, rep=1)], "A", "B", ["t1"])
    assert mixed.tasks[0].category == "mixed"

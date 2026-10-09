"""Improvement tasks under an equal evaluation budget: every objective measurement counts, the
best verified value so far is tracked after each one, and an anytime score rewards good results
found early (after AgenticBBO-Bench)."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harnesslab.config import Settings
from harnesslab.core.models import VariantSpec
from harnesslab.experiments.aggregate import RunSample, aggregate_variants, samples_from_rows
from harnesslab.experiments.stats import paired_comparison
from harnesslab.improve.evaluator import EVAL_DIR, install_evaluator, read_values
from harnesslab.improve.protocol import anytime_score
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.test_improve import ScriptedRunner, _run, _score, _score_task


def test_the_evaluator_records_what_it_measured(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "n.txt").write_text("7")
    install_evaluator(
        work,
        command="cat n.txt",
        inject=[],
        budget=2,
        repeats=1,
        timeout=30,
        unit=None,
        direction="minimize",
    )
    run = lambda: subprocess.run(  # noqa: E731
        [sys.executable, str(work / EVAL_DIR / "evaluate.py")], cwd=work, capture_output=True
    )
    run()
    (work / "n.txt").write_text("5")
    run()
    assert read_values(work) == {1: 7.0, 2: 5.0}
    # A call killed before it logged its value leaves a gap; later values keep their call number.
    (work / EVAL_DIR / "values.jsonl").write_text('{"call": 2, "value": 5}\n')
    assert read_values(work) == {2: 5.0}


def test_anytime_score_rewards_early_results():
    # Scores of the best verified value after each evaluation, the budget, the final score.
    early = anytime_score([0.9, 0.9, 0.9, 0.9], budget=4, final=0.9)
    late = anytime_score([0.0, 0.0, 0.0, 0.9], budget=4, final=0.9)
    assert early == pytest.approx(0.9) and late == pytest.approx(0.7 * 0.225 + 0.3 * 0.9)
    # Evaluations left unused keep the last best: stopping early is not penalised.
    assert anytime_score([0.5, 0.8], budget=4, final=0.8) == pytest.approx(0.7 * 0.725 + 0.3 * 0.8)
    assert anytime_score([], budget=0, final=0.0) is None


async def test_max_evaluations_caps_rounds_and_in_loop_budgets(settings: Settings, db: Database):
    ScriptedRunner.steps = [_score(4), _score(3), _score(2)]
    seen = []

    class Recording(ScriptedRunner):
        async def run(self, task, worktree, config, emit):
            seen.append(task.prompt)
            return await super().run(task, worktree, config, emit)

    update = _score_task(3)
    improve = update["improve"]
    update["improve"] = improve.model_copy(
        update={
            "max_evaluations": 2,
            "evaluator": improve.evaluator.model_copy(update={"budget": 2}),
        }
    )
    variant = VariantSpec(id="capped", runner="scripted")
    _, outcome = await _run(
        settings,
        db,
        [variant],
        task_update=update,
        factory=lambda name, **kwargs: Recording(**kwargs),
    )
    m = outcome.runs[0].metrics
    # Round 1 may call the evaluator once (2 left, 1 kept for its own evaluation), round 2 not
    # at all, and nothing is left for round 3.
    assert "at most 1 time(s)" in seen[0] and "cannot measure" in seen[1].lower()
    assert len(seen) == 2 and m.improve_rounds == 2 and m.improve_evaluations == 2
    assert m.improve_anytime is not None and 0 < m.improve_anytime <= m.verified_score


async def test_in_loop_measurements_count_and_the_curve_is_recorded(
    settings: Settings, db: Database
):
    from harnesslab.experiments.spec import load_suite
    from tests.test_improve import IMPROVE_SUITE

    suite, _ = load_suite(IMPROVE_SUITE)
    by_id = {v.id: v for v in suite.variants}
    service, outcome = await _run(
        settings,
        db,
        [by_id["fake-improver-evaluating"], by_id["fake-improver"], by_id["fake-noop"]],
    )
    runs = {r.variant_key: r.metrics for r in outcome.runs}
    evaluating = runs["fake-improver-evaluating"]
    assert evaluating.improve_evaluations == 3 + evaluating.evaluator_calls
    assert runs["fake-improver"].improve_evaluations == 3
    assert runs["fake-noop"].improve_anytime == 0.0
    assert runs["fake-improver"].improve_anytime > runs["fake-improver-evaluating"].improve_anytime
    row = service.repo.get_run(outcome.runs[0].run_id)
    kinds = {p["kind"] for p in row.metrics_json["improve_evaluation_curve"]}
    assert "round" in kinds
    exp = service.repo.get_experiment(outcome.experiment_id)
    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    aggs = aggregate_variants(samples, ["fake-improver", "fake-noop"])
    assert aggs["fake-improver"].improve_anytime.median > 0
    with TestClient(create_app(settings, db)) as client:
        assert "anytime" in client.get(f"/experiments/{outcome.experiment_id}").text
        compare = client.get(
            f"/experiments/{outcome.experiment_id}/compare",
            params={"a": "fake-improver", "b": "fake-improver-evaluating", "metric": "anytime"},
        ).text
        # The verdict's own interval row, not only the metric picker above it.
        assert '<strong>anytime score</strong> <span class="muted small">(verdict)' in compare


def test_a_verdict_on_the_anytime_score():
    samples = []
    for i in range(6):
        samples.append(
            RunSample(
                run_id=f"a{i}",
                task_key=f"t{i}",
                variant_key="A",
                verified_pass=True,
                improve_anytime=0.5,
            )
        )
        samples.append(
            RunSample(
                run_id=f"b{i}",
                task_key=f"t{i}",
                variant_key="B",
                verified_pass=True,
                improve_anytime=0.8,
            )
        )
    result = paired_comparison(samples, "A", "B", metric="anytime")
    assert result.verdict == "better" and result.n_metric_tasks == 6
    assert paired_comparison(samples, "A", "B").verdict == "no evidence"  # pass rates tie


def test_the_anytime_interval_is_printed_with_the_others():
    from harnesslab import cli

    samples = [
        RunSample(
            run_id=f"{v}{i}",
            task_key=f"t{i}",
            variant_key=v,
            verified_pass=True,
            improve_anytime=0.5 if v == "A" else 0.8,
        )
        for i in range(6)
        for v in ("A", "B")
    ]
    with cli.console.capture() as captured:
        cli._print_paired(paired_comparison(samples, "A", "B", metric="anytime"))
    assert "  anytime score: +0.30" in captured.get()


def test_a_broken_final_state_gets_no_final_credit():
    from harnesslab.core.models import Outcome, RunMetrics, VerifierResult
    from harnesslab.experiments.service import _apply_improvement
    from harnesslab.improve.protocol import EvaluationPoint, ImproveResult

    result = ImproveResult(
        direction="minimize",
        rounds_planned=1,
        evaluator_budget=0,
        baseline=4.0,
        best=2.0,
        final=2.0,
        score=0.5,
        evaluations=[EvaluationPoint(index=1, round=1, kind="round", value=2.0, best=2.0)],
    )
    result.anytime = result.anytime_given(result.score)
    assert result.anytime == pytest.approx(0.5)
    passed, broken = RunMetrics(), RunMetrics()
    _apply_improvement(passed, result, VerifierResult(passed=True), Outcome.PASS)
    _apply_improvement(broken, result, VerifierResult(passed=False), Outcome.FAIL)
    assert passed.improve_anytime == pytest.approx(0.5)
    assert broken.verified_score == 0.0 and broken.improve_anytime == pytest.approx(0.35)


def test_a_resumed_round_without_evaluations_says_so():
    from harnesslab.experiments.spec import load_suite
    from harnesslab.improve.protocol import build_resume_prompt
    from tests.test_improve import IMPROVE_SUITE

    _, tasks = load_suite(IMPROVE_SUITE)
    prompt = build_resume_prompt(
        tasks[0], 2, 3, baseline=5.0, best=4.0, best_round=1, unseen=[], budget=0
    )
    assert "cannot measure" in prompt.lower()

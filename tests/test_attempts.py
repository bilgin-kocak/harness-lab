"""Several attempts per task: pass@k, best-of-k picked by the visible check, and the visible
check itself (run before the hidden files are injected)."""

import itertools
import random
from fractions import Fraction
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.bundled import bundled_suite_path
from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, VariantSpec
from harnesslab.experiments.aggregate import RunSample, samples_from_rows
from harnesslab.experiments.attempts import attempts_report, best_of_k, pass_at_k
from harnesslab.experiments.export import export_experiment
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.test_cli import _env

DEMO = bundled_suite_path("demo")


# -- the estimators ------------------------------------------------------------------------


def _brute_pass_at_k(passes: list[bool], k: int) -> Fraction:
    subsets = list(itertools.combinations(passes, k))
    return Fraction(sum(1 for s in subsets if any(s)), len(subsets))


def _brute_best_of_k(attempts: list[tuple[bool, bool]], k: int) -> Fraction:
    """Every k-subset equally likely; pick uniformly among its visible passes, else among all."""
    total = Fraction(0)
    subsets = list(itertools.combinations(attempts, k))
    for subset in subsets:
        pool = [a for a in subset if a[0]] or list(subset)
        total += Fraction(sum(1 for a in pool if a[1]), len(pool))
    return total / len(subsets)


def test_pass_at_k_is_the_chance_that_one_of_k_attempts_passes():
    assert pass_at_k(5, 2, 1) == pytest.approx(0.4)
    assert pass_at_k(5, 2, 5) == 1.0 and pass_at_k(5, 0, 3) == 0.0
    assert pass_at_k(3, 1, 2) == pytest.approx(2 / 3)
    rng = random.Random(7)
    for _ in range(40):
        n = rng.randint(1, 7)
        passes = [rng.random() < 0.4 for _ in range(n)]
        for k in range(1, n + 1):
            assert pass_at_k(n, sum(passes), k) == pytest.approx(float(_brute_pass_at_k(passes, k)))
    with pytest.raises(ValueError):
        pass_at_k(2, 1, 3)


def test_best_of_k_picks_by_the_visible_check():
    # A real fix (visible and hidden pass) and a broken attempt: picking by visible tests wins.
    assert best_of_k([(True, True), (False, False)], 2) == 1.0
    assert best_of_k([(True, True), (False, False)], 1) == pytest.approx(0.5)  # = pass rate
    # The visible check is fooled by an attempt that passes it but fails the hidden tests.
    assert best_of_k([(True, False), (True, True), (False, False)], 3) == pytest.approx(0.5)
    # No attempt passes the visible check: the pick is a random attempt.
    assert best_of_k([(False, True), (False, False)], 2) == pytest.approx(0.5)
    rng = random.Random(11)
    for _ in range(60):
        n = rng.randint(1, 7)
        attempts = [(rng.random() < 0.6, rng.random() < 0.5) for _ in range(n)]
        for k in range(1, n + 1):
            assert best_of_k(attempts, k) == pytest.approx(float(_brute_best_of_k(attempts, k)))


# -- the report ----------------------------------------------------------------------------


def _s(task, variant, rep, passed, visible=None):
    return RunSample(
        run_id=f"{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        verified_pass=passed,
        visible_pass=visible,
    )


def test_attempts_report_per_variant():
    tasks = [f"t{i}" for i in range(6)]
    samples = []
    for t in tasks:  # attempt 0 breaks things, attempt 1 fixes it, attempt 2 changes nothing
        samples += [
            _s(t, "flaky", 0, False, False),
            _s(t, "flaky", 1, True, True),
            _s(t, "flaky", 2, False, True),
        ]
        samples += [_s(t, "blind", r, r == 1) for r in range(3)]  # no visible check recorded
        samples += [_s(t, "once", 0, True, True)]
    report = {r.variant_key: r for r in attempts_report(samples, ["flaky", "blind", "once"], tasks)}

    flaky = report["flaky"]
    assert flaky.k_max == 3 and flaky.n_tasks == 6 and flaky.n_best_tasks == 6
    rows = {row.k: row for row in flaky.rows}
    assert rows[1].pass_at_k == pytest.approx(1 / 3) and rows[3].pass_at_k == 1.0
    assert rows[1].best_of_k == pytest.approx(1 / 3) and rows[3].best_of_k == pytest.approx(0.5)
    assert flaky.selector_gain is not None and flaky.selector_gain.estimate == pytest.approx(1 / 6)
    assert flaky.verdict == "better"  # the same gain on every task: the interval excludes 0

    blind = report["blind"]
    assert blind.rows[-1].best_of_k is None and "visible_command" in (blind.note or "")
    once = report["once"]
    assert once.k_max == 1 and "2 attempts" in (once.note or "")


def test_tasks_with_fewer_attempts_set_k_and_invalid_runs_do_not_count():
    samples = [_s("a", "v", r, r == 0, True) for r in range(4)] + [
        _s("b", "v", 0, True, True),
        _s("b", "v", 1, False, False),
        RunSample(run_id="b-v-2", task_key="b", variant_key="v", repetition=2),  # not verified
    ]
    (report,) = attempts_report(samples, ["v"], ["a", "b"])
    assert report.k_max == 2 and report.n_tasks == 2


# -- the visible check and the demo ---------------------------------------------------------


async def _run(settings, db, variants, *, repetitions=1, task_update=None, suite_path=DEMO):
    suite, tasks = load_suite(suite_path)
    if task_update is not None:
        tasks = [t.model_copy(update=task_update(t), deep=True) for t in tasks]
    service = ExperimentService(settings, db)
    outcome = await service.run_experiment(
        ExperimentSpec(
            name="attempts",
            suite=str(suite_path),
            source_path=suite_path,
            repetitions=repetitions,
            parallelism=4,
        ),
        suite,
        tasks,
        variants,
    )
    return service, outcome


async def test_the_visible_check_runs_before_the_hidden_files_arrive(
    settings: Settings, db: Database
):
    def update(task):
        hidden = task.verification.inject[0].dest
        check = f"test ! -e {hidden} && python -m unittest discover -s tests"
        return {"verification": task.verification.model_copy(update={"visible_command": check})}

    variants = [
        VariantSpec(id="fixer", runner="fake", behavior="solve"),
        VariantSpec(id="breaker", runner="fake", behavior="fail"),
    ]
    service, outcome = await _run(settings, db, variants, task_update=update)
    by = {(r.task_key, r.variant_key): r for r in outcome.runs}
    fixed = by[("fix-month-boundary", "fixer")]
    assert fixed.metrics.visible_pass is True and fixed.metrics.verified_pass is True
    assert by[("fix-month-boundary", "breaker")].metrics.visible_pass is False
    row = service.repo.get_run(fixed.run_id)
    assert row.visible_pass is True and row.verifier_result.visible_passed is True
    exp = service.repo.get_experiment(outcome.experiment_id)
    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    assert {s.visible_pass for s in samples} == {True, False}


async def test_improvement_evaluations_do_not_run_the_visible_check(
    settings: Settings, db: Database, tmp_path: Path
):
    calls = tmp_path / "visible-calls"
    improve_suite = bundled_suite_path("demo-improve")

    def update(task):
        check = f"echo call >> {calls} && python -m unittest discover -s tests"
        return {"verification": task.verification.model_copy(update={"visible_command": check})}

    variant = VariantSpec(id="improver", runner="fake", behavior="solve", improve_rounds=2)
    _, outcome = await _run(settings, db, [variant], task_update=update, suite_path=improve_suite)
    assert outcome.runs[0].metrics.improve_rounds == 2
    assert calls.read_text().count("call") == 1  # the final verification only


async def test_flaky_demo_variant_shows_what_more_attempts_are_worth(
    settings: Settings, db: Database
):
    suite, _ = load_suite(DEMO)
    flaky = next(v for v in suite.variants if v.id == "fake-flaky")
    service, outcome = await _run(settings, db, [flaky], repetitions=3)
    by_rep = {(r.task_key, r.repetition): r for r in outcome.runs}
    assert [by_rep[("fix-month-boundary", i)].metrics.verified_pass for i in range(3)] == [
        False,
        True,
        False,
    ]
    exp = service.repo.get_experiment(outcome.experiment_id)
    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    (report,) = attempts_report(samples, ["fake-flaky"], [t.task_key for t in exp.tasks])
    rows = {row.k: row for row in report.rows}
    assert rows[1].pass_at_k == pytest.approx(1 / 3) and rows[3].pass_at_k == 1.0
    assert rows[1].pass_at_k <= rows[3].best_of_k <= rows[3].pass_at_k

    exported = export_experiment(service.repo, outcome.experiment_id)
    assert exported["attempts"][0]["variant_key"] == "fake-flaky"
    with TestClient(create_app(settings, db)) as client:
        page = client.get(f"/experiments/{outcome.experiment_id}")
        assert page.status_code == 200 and "best-of-3" in page.text


def test_cli_prints_attempts_after_a_run_and_in_experiment_show(tmp_path: Path):
    runner = CliRunner()
    env = _env(tmp_path)
    result = runner.invoke(
        app,
        [
            "run",
            "demo",
            "--variants",
            "fake-flaky",
            "--repetitions",
            "3",
            "--tasks",
            "fix-month-boundary",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "pass@3" in result.output and "best-of-3" in result.output
    exp_id = next(
        line.split()[-1] for line in result.output.splitlines() if "experiment id" in line
    )
    show = runner.invoke(app, ["experiment", "show", exp_id], env=env)
    assert show.exit_code == 0 and "best-of-3" in show.output, show.output

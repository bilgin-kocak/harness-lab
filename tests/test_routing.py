"""Per-task selection: in-sample routing gap, held-out gain, sweep and experiment surfaces."""

import random
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.models import SweepSpec
from harnesslab.experiments.aggregate import RunSample
from harnesslab.experiments.routing import routing_gap
from harnesslab.experiments.sweep import analyze_sweep
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.test_cli import _env

runner = CliRunner()


def _s(task, variant, passed, rep=0):
    return RunSample(
        run_id=f"{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        verified_pass=passed,
    )


def _grid(passes: dict[str, set[str]], tasks: list[str], reps: int) -> list[RunSample]:
    """Deterministic runs: variant v passes task t on every repetition iff t in passes[v]."""
    return [
        _s(t, v, t in solved, rep)
        for v, solved in passes.items()
        for t in tasks
        for rep in range(reps)
    ]


TASKS = [f"t{i}" for i in range(6)]
SPECIALISED = {"A": {"t0", "t1", "t2"}, "B": {"t3", "t4"}, "C": {"t5"}}


def test_specialised_variants_gain_in_sample_and_held_out():
    gap = routing_gap(_grid(SPECIALISED, TASKS, reps=4), TASKS, ["A", "B", "C"])
    assert gap.n_tasks == 6 and gap.n_excluded == 0
    assert gap.best_single == "A" and gap.best_single_rate == pytest.approx(0.5)
    assert gap.oracle_rate == pytest.approx(1.0) and gap.gap == pytest.approx(0.5)
    assert gap.n_improvable == 3
    assert gap.per_task_best == {
        "t0": ["A"],
        "t1": ["A"],
        "t2": ["A"],
        "t3": ["B"],
        "t4": ["B"],
        "t5": ["C"],
    }
    # The specialisation is real, so it survives selection on one half and scoring on the other.
    assert gap.n_held_out_tasks == 6 and gap.held_out_gain == pytest.approx(0.5)
    assert gap.note is None
    line = gap.summary()
    assert "best single A 50%" in line and "best per task 100%" in line
    assert "+50 points, 3 of 6 tasks" in line and "held out +50 points" in line


def test_identical_noisy_variants_gap_is_selection_bias():
    rng = random.Random(11)
    tasks = [f"t{i}" for i in range(40)]
    variants = ["A", "B", "C"]
    samples = [
        _s(t, v, rng.random() < 0.5, rep) for v in variants for t in tasks for rep in range(4)
    ]
    gap = routing_gap(samples, tasks, variants)
    # Picking the per-task maximum of three coin flips looks like a large gain in sample ...
    assert gap.gap > 0.15 and gap.n_improvable > 10
    # ... which disappears once the choice is scored on runs it was not made on.
    assert gap.n_held_out_tasks == 40
    assert gap.held_out_gain is not None and gap.held_out_gain < 0.05
    assert gap.held_out_gain < gap.gap / 3


def test_single_repetition_has_no_held_out_estimate():
    gap = routing_gap(_grid(SPECIALISED, TASKS, reps=1), TASKS, ["A", "B", "C"])
    assert gap.gap == pytest.approx(0.5)
    assert gap.held_out_gain is None and gap.n_held_out_tasks == 0
    assert "needs at least 2 repetitions" in gap.note
    assert "held out: needs at least 2 repetitions" in gap.summary()


def test_incomplete_tasks_are_left_out():
    samples = _grid({"A": {"t0"}, "B": {"t1"}}, ["t0", "t1"], reps=2)
    samples += [_s("t2", "A", True), _s("t2", "A", True, rep=1)]  # B never ran t2
    samples += [_s("t3", "A", True), _s("t3", "B", None)]  # B has no verdict on t3
    samples += [_s("t4", "A", True), _s("t4", "B", False)]  # one repetition only
    samples += [_s("other", "A", True), _s("other", "B", True), _s("t0", "Z", True)]
    gap = routing_gap(samples, ["t0", "t1", "t2", "t3", "t4", "t5"], ["A", "B"])
    assert gap.n_tasks == 3 and gap.n_excluded == 3  # t2, t3 and t5 (no runs at all)
    assert set(gap.per_task_best) == {"t0", "t1", "t4"}
    assert gap.best_single == "A" and gap.best_single_rate == pytest.approx(2 / 3)
    assert gap.oracle_rate == pytest.approx(1.0) and gap.n_improvable == 1
    # t4 has no odd repetition, so only t0 and t1 are scored out of sample.
    assert gap.n_held_out_tasks == 2 and gap.held_out_gain == pytest.approx(0.5)
    assert "3 tasks left out" in gap.summary()


def test_ties_go_to_the_earlier_variant():
    samples = _grid({"A": {"t0", "t1"}, "B": {"t0", "t1"}}, ["t0", "t1", "t2"], reps=2)
    gap = routing_gap(samples, ["t0", "t1", "t2"], ["A", "B"])
    assert gap.best_single == "A" and gap.gap == 0.0 and gap.n_improvable == 0
    assert gap.per_task_best == {"t0": ["A", "B"], "t1": ["A", "B"], "t2": ["A", "B"]}
    assert gap.held_out_gain == 0.0
    assert routing_gap(samples, ["t0", "t1", "t2"], ["B", "A"]).best_single == "B"


def test_fewer_than_two_variants_or_no_complete_task():
    samples = _grid({"A": {"t0"}}, ["t0"], reps=2)
    assert routing_gap(samples, ["t0"], ["A"]) is None
    assert routing_gap(samples, ["t0"], []) is None
    empty = routing_gap(samples, ["t0"], ["A", "B"])
    assert empty.n_tasks == 0 and empty.n_excluded == 1 and empty.best_single is None
    assert empty.gap is None and empty.held_out_gain is None
    assert "no task has valid runs from every variant" in empty.summary()


def test_analyze_sweep_attaches_routing_per_workload():
    spec = SweepSpec(
        name="s",
        suite="demo",
        base_variant={"runner": "fake"},
        factors={"g": ["a", "b", "c"]},
        repetitions=4,
    )
    keys = {"A": "g=a", "B": "g=b", "C": "g=c"}
    samples = [
        s.model_copy(update={"variant_key": keys[s.variant_key]})
        for s in _grid(SPECIALISED, TASKS, reps=4)
    ]
    factors = {v: {"g": v.split("=")[1]} for v in keys.values()}
    report = analyze_sweep(spec, samples, factors, {t: [] for t in TASKS})
    routing = report.workloads[0].routing
    assert routing.best_single == "g=a" and routing.held_out_gain == pytest.approx(0.5)
    assert report.model_dump(mode="json")["workloads"][0]["routing"]["n_tasks"] == 6


SWEEP_YAML = """\
name: routing-demo
suite: demo
base_variant: { runner: fake, behavior: solve }
factors:
  solver:
    bugfix: { solve_tasks: [fix-month-boundary] }
    features: { solve_tasks: [add-tag-budgets, consolidate-money-formatting] }
repetitions: 2
parallelism: 3
workload_by: suite
objective:
  require: { min_pass_rate: 0.5 }
  minimize: cost
"""


def test_sweep_and_experiment_surfaces_print_the_routing_line(tmp_path: Path):
    env = _env(tmp_path)
    sweep_file = tmp_path / "routing.yaml"
    sweep_file.write_text(SWEEP_YAML, encoding="utf-8")
    result = runner.invoke(app, ["sweep", "run", str(sweep_file)], env=env)
    assert result.exit_code == 0, result.output
    exp_id = re.search(r"experiment id: (exp_[a-z0-9]+)", result.output).group(1)
    expected = (
        "per-task selection: best single solver=features 67% · best per task 100% "
        "(+33 points, 1 of 3 tasks) · held out +33 points"
    )
    for args in (["sweep", "report", exp_id], ["experiment", "show", exp_id]):
        shown = runner.invoke(app, args, env=env)
        assert shown.exit_code == 0, shown.output
        assert expected in shown.output

    hl = Settings(home=Path(env["HARNESSLAB_HOME"]))
    db = Database(hl.resolved_database_url)
    with TestClient(create_app(hl, db)) as client:
        page = client.get(f"/experiments/{exp_id}")
        assert page.status_code == 200 and "Per-task selection" in page.text
        assert "solver=features" in page.text and "+33 points" in page.text
        data = client.get(
            f"/api/experiments/{exp_id}/export.json?events=false&artifacts=false"
        ).json()
        assert data["routing"]["best_single"] == "solver=features"
        assert data["routing"]["held_out_gain"] == pytest.approx(1 / 3)
        assert data["sweep_report"]["workloads"][0]["routing"]["n_improvable"] == 1
    db.dispose()

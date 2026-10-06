"""Improvement tasks: objective math, checkpoints, the in-loop evaluator and the round protocol."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.bundled import bundled_suite_path
from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.events import EventKind
from harnesslab.core.models import (
    ExperimentSpec,
    Outcome,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    TaskSpec,
    VariantSpec,
)
from harnesslab.experiments.aggregate import aggregate_variants, compare_variants, samples_from_rows
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.improve.checkpoint import exclude_in_git, restore, snapshot
from harnesslab.improve.evaluator import EVAL_DIR, count_calls, install_evaluator, reset_calls
from harnesslab.improve.objective import (
    improvement_ratio,
    improvement_score,
    is_better,
    parse_value,
    progress,
)
from harnesslab.improve.protocol import RoundRecord, build_round_prompt
from harnesslab.runners.base import HarnessRunner
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.test_cli import _env

IMPROVE_SUITE = bundled_suite_path("demo-improve")


# -- objective math ------------------------------------------------------------------


def test_parse_value():
    assert parse_value("65440\n") == 65440.0
    assert parse_value("n=400\nkey operations: 880\n") == 880.0
    assert parse_value('noise 3\n{"value": 2.5, "unit": "s"}\n') == 2.5
    assert parse_value('{"value": true}\n7') == 7.0
    assert parse_value("1.5e3") == 1500.0
    assert parse_value("no numbers here") is None
    assert parse_value("") is None


def test_improvement_math():
    assert is_better(25, 100, "minimize") and not is_better(100, 100, "minimize")
    assert not is_better(99.5, 100, "minimize", 0.01) and is_better(98, 100, "minimize", 0.01)
    assert is_better(30, 10, "maximize") and not is_better(9, 10, "maximize")
    assert improvement_ratio(100, 25, "minimize") == 4.0
    assert improvement_ratio(10, 30, "maximize") == 3.0
    assert (
        improvement_ratio(0, 5, "minimize") is None and improvement_ratio(-1, 5, "maximize") is None
    )
    assert improvement_score(4.0) == 0.75 and improvement_score(1.0) == 0.0
    assert improvement_score(0.5) == 0.0 and improvement_score(None) == 0.0
    assert progress(100, 60, 20) == 0.5 and progress(10, 30, 50) == 0.5
    assert progress(100, 60, None) is None and progress(100, 60, 100) is None


# -- checkpoints -----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_snapshot_and_restore(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    exclude_in_git(repo, f"{EVAL_DIR}/")
    exclude_in_git(repo, f"{EVAL_DIR}/")  # idempotent
    (repo / EVAL_DIR).mkdir()
    (repo / EVAL_DIR / "calls.jsonl").write_text("x\n")

    (repo / "a.txt").write_text("round one\n")
    (repo / "b.txt").write_text("new file\n")
    best = snapshot(repo, base, "round 1")
    assert _git(repo, "rev-parse", "HEAD") == base  # the agent's HEAD and index are untouched

    (repo / "a.txt").write_text("worse\n")
    (repo / "c.txt").write_text("junk\n")
    restore(repo, best)
    assert (repo / "a.txt").read_text() == "round one\n" and (repo / "b.txt").exists()
    assert not (repo / "c.txt").exists()
    assert (repo / EVAL_DIR / "calls.jsonl").exists()  # excluded files survive a revert
    restore(repo, base)
    assert (repo / "a.txt").read_text() == "base\n" and not (repo / "b.txt").exists()
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert EVAL_DIR not in status


# -- the in-loop evaluator ---------------------------------------------------------------


def test_evaluator_budget_and_injection(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "data.txt").write_text("abcdef")
    objective = tmp_path / "suite" / "bench.py"
    objective.parent.mkdir()
    objective.write_text("print(len(open('data.txt').read()))\n")
    install_evaluator(
        work,
        command=f"{sys.executable} .obj/bench.py",
        inject=[(objective, ".obj/bench.py")],
        budget=2,
        repeats=1,
        timeout=30,
        unit="chars",
        direction="minimize",
    )
    script = work / EVAL_DIR / "evaluate.py"

    def call():
        return subprocess.run(
            [sys.executable, str(script)], cwd=work, capture_output=True, text=True, timeout=60
        )

    first = call()
    assert first.returncode == 0 and "6" in first.stdout and "1 call" in first.stdout
    assert not (work / ".obj").exists()  # measured in a scratch copy, never in the worktree
    assert call().returncode == 0
    third = call()
    assert third.returncode == 3 and "budget" in third.stderr
    assert count_calls(work) == 2
    reset_calls(work)
    assert count_calls(work) == 0 and call().returncode == 0


def test_round_prompt_reports_history_without_hidden_detail():
    task = TaskSpec(
        id="t",
        name="t",
        repo={"path": "."},
        prompt="Make it faster.",
        verification={"command": "true"},
        improve={
            "objective": {"command": "python bench.py", "unit": "ops", "direction": "minimize"},
            "rounds": 3,
        },
    )
    history = [
        RoundRecord(
            round=1, status="completed", value=880, gate_passed=True, best=880, accepted=True
        ),
    ]
    prompt = build_round_prompt(
        task, 2, 3, baseline=65440, best=880, best_round=1, history=history, budget=2
    )
    assert prompt.startswith("Make it faster.")
    assert "round 2 of 3" in prompt.lower() and "65440" in prompt and "880" in prompt
    assert "lower is better" in prompt.lower() and ".harnesslab_eval/evaluate.py" in prompt
    blind = build_round_prompt(
        task, 1, 3, baseline=65440, best=65440, best_round=0, history=[], budget=0
    )
    assert "evaluate.py" not in blind and "cannot measure" in blind.lower()
    failed = RoundRecord(
        round=1, status="completed", value=None, gate_passed=False, best=65440, reverted=True
    )
    text = build_round_prompt(
        task, 2, 3, baseline=65440, best=65440, best_round=0, history=[failed], budget=0
    )
    assert "correctness checks failed" in text and "reverted" in text


# -- the protocol, end to end ----------------------------------------------------------


async def _run(settings, db, variants, *, task_update=None, factory=None):
    suite, tasks = load_suite(IMPROVE_SUITE)
    task = tasks[0] if task_update is None else tasks[0].model_copy(update=task_update, deep=True)
    kwargs = {"runner_factory": factory} if factory else {}
    service = ExperimentService(settings, db, **kwargs)
    outcome = await service.run_experiment(
        ExperimentSpec(
            name="imp", suite=str(IMPROVE_SUITE), parallelism=4, source_path=IMPROVE_SUITE
        ),
        suite,
        [task],
        variants,
    )
    return service, outcome


async def test_demo_improve_end_to_end(settings: Settings, db: Database):
    suite, _ = load_suite(IMPROVE_SUITE)
    by_id = {v.id: v for v in suite.variants}
    variants = [
        by_id[k] for k in ("fake-improver", "fake-noop", "fake-breaker", "fake-improver-evaluating")
    ]
    service, outcome = await _run(settings, db, variants)
    runs = {r.variant_key: r for r in outcome.runs}

    improver = runs["fake-improver"]
    m = improver.metrics
    assert improver.outcome == Outcome.PASS and improver.status == RunStatus.COMPLETED
    assert (m.improve_baseline, m.improve_best, m.improve_final) == (65440, 440, 440)
    assert m.improve_curve == [65440, 880, 440, 440] and m.improve_rounds == 3
    assert round(m.improve_ratio, 2) == 148.73 and m.improve_progress == 1.0
    assert m.verified_pass is True and round(m.verified_score, 4) == round(1 - 440 / 65440, 4)
    assert m.evaluator_calls == 0 and m.llm_calls == 9  # three rounds of the fake agent

    noop = runs["fake-noop"].metrics
    assert runs["fake-noop"].outcome == Outcome.FAIL and noop.verified_pass is False
    assert noop.improve_curve == [65440, 65440, 65440, 65440] and noop.improve_ratio == 1.0
    assert noop.verified_score == 0.0

    breaker = runs["fake-breaker"]
    assert breaker.outcome == Outcome.PASS and breaker.metrics.improve_final == 440
    history = breaker.metrics.improve_history
    assert history[0]["gate_passed"] is False and history[0]["reverted"] is True
    assert history[1]["accepted"] is True and history[1]["value"] == 440

    assert runs["fake-improver-evaluating"].metrics.evaluator_calls == 6

    row = service.repo.get_run(improver.run_id)
    kinds = {a.kind for a in row.artifacts}
    assert "improve" in kinds
    improve_doc = json.loads(
        service.repo.read_artifact(next(a for a in row.artifacts if a.kind == "improve"))
    )
    assert improve_doc["improved"] is True and len(improve_doc["rounds"]) == 3
    diff = service.repo.read_artifact(next(a for a in row.artifacts if a.kind == "agent_diff"))
    assert "setdefault" in diff and EVAL_DIR not in diff and "harnesslab_objective" not in diff
    names = [e.name for e in row.events if e.kind == "system"]
    assert names.count("improve_round") == 3 and "improve_baseline" in names
    assert row.improve_ratio and row.evaluator_calls == 0

    exp = service.repo.get_experiment(outcome.experiment_id)
    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    aggs = aggregate_variants(samples, [v.id for v in variants])
    assert round(aggs["fake-improver"].improve_ratio.median, 2) == 148.73
    assert aggs["fake-noop"].improve_ratio.median == 1.0
    cmp = compare_variants(samples, "fake-noop", "fake-improver", ["fewer-key-operations"])
    assert (
        cmp.paired.improve_ratio_diff is not None and cmp.paired.improve_ratio_diff.estimate > 100
    )

    with TestClient(create_app(settings, db)) as client:
        page = client.get(f"/runs/{improver.run_id}")
        assert page.status_code == 200 and "Improvement" in page.text and "148.7" in page.text
        exp_page = client.get(f"/experiments/{outcome.experiment_id}")
        assert "median improvement" in exp_page.text


class ProbeRunner(HarnessRunner):
    """Records what the agent's worktree contains at the start of every round."""

    name = "probe"
    seen: list[dict] = []

    async def run(self, task, worktree, config, emit):
        ProbeRunner.seen.append(
            {
                "round": config.improve_round,
                "hidden_test": (worktree / "tests" / "test_hidden_dedupe.py").exists(),
                "objective": (worktree / ".harnesslab_objective").exists(),
                "evaluator": (worktree / EVAL_DIR / "evaluate.py").exists(),
                "prompt": task.prompt,
            }
        )
        emit.emit(EventKind.ASSISTANT_MESSAGE, payload={"text": "probing"})
        return RunnerResult(status=RunStatus.COMPLETED, exit_code=0, llm_calls=1)


async def test_hidden_files_never_reach_the_agent_and_variant_overrides(
    settings: Settings, db: Database
):
    ProbeRunner.seen = []

    def factory(name, **kwargs):
        return ProbeRunner(**kwargs)

    variant = VariantSpec(id="probe", runner="probe", improve_rounds=2, improve_eval_budget=1)
    _, outcome = await _run(settings, db, [variant], factory=factory)
    assert [s["round"] for s in ProbeRunner.seen] == [1, 2]
    assert not any(s["hidden_test"] or s["objective"] for s in ProbeRunner.seen)
    assert all(s["evaluator"] for s in ProbeRunner.seen)
    assert "Round 2 of 2" in ProbeRunner.seen[1]["prompt"]
    run = outcome.runs[0]
    assert run.metrics.improve_rounds == 2 and run.outcome == Outcome.FAIL  # nothing improved

    ProbeRunner.seen = []
    blind = VariantSpec(id="blind", runner="probe", improve_rounds=1, improve_eval_budget=0)
    await _run(settings, db, [blind], factory=factory)
    assert ProbeRunner.seen[0]["evaluator"] is False


async def test_keep_best_false_keeps_the_broken_final_state(settings: Settings, db: Database):
    breaker = VariantSpec(
        id="late-break", runner="fake", behavior="solve", improve_break_rounds=[3]
    )
    suite, tasks = load_suite(IMPROVE_SUITE)
    improve = tasks[0].improve.model_copy(update={"keep_best": False})
    _, outcome = await _run(settings, db, [breaker], task_update={"improve": improve})
    run = outcome.runs[0]
    assert run.outcome == Outcome.FAIL and run.metrics.improve_final is None
    assert run.metrics.improve_best == 440  # the curve still records the best checkpoint
    _, kept = await _run(settings, db, [breaker])
    assert kept.runs[0].outcome == Outcome.PASS and kept.runs[0].metrics.improve_final == 440


async def test_baseline_must_pass_the_gate(settings: Settings, db: Database):
    suite, tasks = load_suite(IMPROVE_SUITE)
    verification = tasks[0].verification.model_copy(update={"command": "exit 1"})
    variant = VariantSpec(id="ref", runner="fake", behavior="solve")
    _, outcome = await _run(settings, db, [variant], task_update={"verification": verification})
    run = outcome.runs[0]
    assert run.outcome == Outcome.NOT_VERIFIED and "baseline" in (run.error or "")


def test_suite_check_and_list_on_demo_improve(tmp_path: Path):
    runner = CliRunner()
    env = _env(tmp_path)
    check = runner.invoke(app, ["suite", "check", "demo-improve"], env=env)
    assert check.exit_code == 0, check.output
    listing = runner.invoke(app, ["suite", "list", "demo-improve"], env=env)
    assert listing.exit_code == 0 and "fewer-key-operations" in listing.output


def test_runner_config_round_is_not_part_of_the_config_hash():
    a = RunnerConfig(runner="fake", options={"x": 1})
    b = a.model_copy(update={"improve_round": 3})
    assert a.config_hash() == b.config_hash() and b.improve_round == 3


def test_task_spec_rejects_unknown_improve_keys():
    with pytest.raises(ValueError):
        TaskSpec(
            id="t",
            name="t",
            repo={"path": "."},
            prompt="p",
            verification={"command": "true"},
            improve={"objective": {"command": "x"}, "roundz": 2},
        )

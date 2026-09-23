"""Configuration sweeps: expansion, budget, analysis, service integration, CLI, dashboard."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnesslab.cli import app
from harnesslab.config import Settings
from harnesslab.core.models import RunnerConfig, RunStatus, SweepSpec, VariantSpec
from harnesslab.experiments.aggregate import RunSample
from harnesslab.experiments.spec import SpecError, load_sweep_target
from harnesslab.experiments.sweep import (
    BudgetGate,
    analyze_sweep,
    expand_sweep,
)
from harnesslab.runners.claude import build_claude_command
from harnesslab.storage.database import Database
from harnesslab.web.app import create_app
from tests.conftest import DEMO_SUITE, FIXTURES
from tests.test_cli import _env

runner = CliRunner()


def _spec(**overrides) -> SweepSpec:
    data = {
        "name": "s",
        "suite": "demo",
        "base_variant": {"runner": "fake", "max_turns": 5},
        "factors": {
            "model": ["m-small", "m-big"],
            "toolset": {
                "min": {"allowed_tools": ["Read"]},
                "full": {"allowed_tools": ["Read", "Edit"]},
            },
        },
        "repetitions": 2,
    }
    data.update(overrides)
    return SweepSpec(**data)


def test_expand_grid_ids_and_merging():
    variants = expand_sweep(_spec())
    assert [v.id for v in variants] == [
        "model=m-small|toolset=min",
        "model=m-small|toolset=full",
        "model=m-big|toolset=min",
        "model=m-big|toolset=full",
    ]
    v = variants[1]
    assert (
        v.runner == "fake"
        and v.model == "m-small"
        and v.factors == {"model": "m-small", "toolset": "full"}
    )
    assert v.options == {"max_turns": 5, "allowed_tools": ["Read", "Edit"]}
    assert "factors" not in v.runner_config().options


def test_expand_sampling_is_deterministic_and_validation():
    a = expand_sweep(_spec(sample={"max_configs": 2, "seed": 3}))
    b = expand_sweep(_spec(sample={"max_configs": 2, "seed": 3}))
    assert len(a) == 2 and [v.id for v in a] == [v.id for v in b]
    assert len(expand_sweep(_spec(sample={"max_configs": 10}))) == 4
    with pytest.raises(ValueError, match="runner"):
        _spec(base_variant={"max_turns": 1})
    with pytest.raises(ValueError, match="no levels"):
        _spec(factors={"model": []})


def test_budget_gate():
    gate = BudgetGate(max_runs=2, max_cost_usd=1.0)
    assert gate.check() is None and gate.check() is None
    assert gate.check().startswith("budget: max_runs")
    gate = BudgetGate(max_cost_usd=0.5)
    assert gate.check() is None
    gate.on_finished(None, 0.6)
    assert "max_cost_usd" in gate.check()


def _sample(
    task, variant, passed, cost=None, est=None, tokens=100, wall=10.0, rep=0, status="completed"
):
    return RunSample(
        run_id=f"{task}-{variant}-{rep}",
        task_key=task,
        variant_key=variant,
        repetition=rep,
        status=status,
        outcome="pass" if passed else ("fail" if passed is not None else "not_verified"),
        verified_pass=passed,
        verified_score=None if passed is None else float(passed),
        wall_time_seconds=wall,
        input_tokens=tokens,
        output_tokens=0,
        cached_input_tokens=0,
        tool_calls=3,
        files_changed=1,
        reported_cost_usd=cost,
        estimated_cost_usd=est,
    )


FACTORS = {
    "A": {"model": "small", "toolset": "min"},
    "B": {"model": "small", "toolset": "full"},
    "C": {"model": "big", "toolset": "min"},
}


def test_analysis_recommends_cheapest_eligible_with_tiebreak_and_pareto():
    spec = _spec(
        repetitions=1,
        objective={
            "require": {"min_pass_rate": 1.0},
            "minimize": "cost",
            "tie_breaker": "wall_time_seconds",
        },
    )
    samples = [
        _sample("t1", "A", True, cost=0.10, wall=5),
        _sample("t2", "A", True, cost=0.10, wall=5),
        _sample("t1", "B", True, cost=0.10, wall=2),  # same cost as A, faster -> wins tie
        _sample("t2", "B", True, cost=0.10, wall=2),
        _sample("t1", "C", False, cost=0.01, wall=1),  # cheapest but fails
        _sample("t2", "C", True, cost=0.01, wall=1),
    ]
    report = analyze_sweep(spec, samples, FACTORS, {"t1": ["python"], "t2": ["python"]})
    assert report.objective_kind == "reported_cost_usd" and len(report.workloads) == 1
    w = report.workloads[0]
    assert w.recommended.variant_key == "B" and w.runner_up.variant_key == "A"
    assert [c.variant_key for c in w.configs][:2] == ["B", "A"]
    c = next(c for c in w.configs if c.variant_key == "C")
    assert not c.eligible and "pass rate 50%" in c.reason
    assert set(w.pareto) == {"A", "B", "C"}  # A and B tie on pass rate and cost: neither dominates
    effects = {(e.factor, e.level): e for e in report.factor_effects}
    assert (
        effects[("model", "small")].mean_pass_rate == 1.0
        and effects[("model", "big")].mean_pass_rate == 0.5
    )
    assert effects[("toolset", "min")].n_configs == 2


def test_analysis_objective_fallbacks_workloads_and_holdout():
    spec = _spec(repetitions=1, workload_by="task", holdout_tasks=["t3"])
    # no reported cost anywhere -> estimated; then tokens
    samples = [
        _sample("t1", "A", True, est=0.2),
        _sample("t2", "A", True, est=0.2),
        _sample("t3", "A", False, est=0.2),
        _sample("t1", "B", True, est=0.5),
        _sample("t2", "B", False, est=0.5),
        _sample("t3", "B", True, est=0.5),
    ]
    tags = {"t1": ["x"], "t2": ["y"], "t3": ["x"]}
    report = analyze_sweep(spec, samples, FACTORS, tags)
    assert report.objective_kind == "estimated_cost_usd"
    assert [w.workload for w in report.workloads] == ["t1", "t2"]
    t2 = report.workloads[1]
    assert (
        t2.recommended.variant_key == "A"
        and t2.holdout.task_keys == ["t3"]
        and t2.holdout.pass_rate == 0.0
    )
    tokens_only = analyze_sweep(
        _spec(repetitions=1),
        [_sample("t1", "A", True, tokens=50), _sample("t1", "B", True, tokens=20)],
        FACTORS,
        {"t1": []},
    )
    assert (
        tokens_only.objective_kind == "total_tokens"
        and tokens_only.workloads[0].recommended.variant_key == "B"
    )
    assert tokens_only.notes and "tokens" in tokens_only.notes[0]
    by_tag = analyze_sweep(_spec(repetitions=1, workload_by="tag"), samples, FACTORS, tags)
    assert [w.workload for w in by_tag.workloads] == ["x", "y"] and by_tag.workloads[
        0
    ].task_keys == ["t1", "t3"]


def test_analysis_none_eligible_and_min_valid_runs():
    spec = _spec(repetitions=2)
    samples = [
        _sample("t1", "A", True, cost=1.0),
        _sample("t1", "A", None, cost=None, status="crashed", rep=1),
        _sample("t1", "B", False, cost=0.1),
        _sample("t1", "B", False, cost=0.1, rep=1),
    ]
    report = analyze_sweep(spec, samples, FACTORS, {"t1": []})
    w = report.workloads[0]
    assert w.recommended is None and w.best_effort.variant_key == "A"
    a = next(c for c in w.configs if c.variant_key == "A")
    assert "only 1 of 2" in a.reason
    relaxed = analyze_sweep(
        _spec(repetitions=2, objective={"require": {"min_pass_rate": 1.0, "min_valid_runs": 1}}),
        samples,
        FACTORS,
        {"t1": []},
    )
    assert relaxed.workloads[0].recommended.variant_key == "A"


async def test_service_gate_skips_runs(settings: Settings, db: Database):
    from harnesslab.core.models import ExperimentSpec
    from harnesslab.experiments.service import ExperimentService
    from harnesslab.experiments.spec import load_suite

    suite, tasks = load_suite(DEMO_SUITE)
    variants = [
        VariantSpec(id="a", runner="fake", behavior="solve", factors={"b": "solve"}),
        VariantSpec(id="b", runner="fake", behavior="noop", factors={"b": "noop"}),
    ]
    gate = BudgetGate(max_runs=2)
    service = ExperimentService(settings, db)
    seen = []
    outcome = await service.run_experiment(
        ExperimentSpec(name="gated", suite="demo", parallelism=1, source_path=DEMO_SUITE),
        suite,
        tasks[:2],
        variants,
        gate=gate.check,
        on_run_finished=seen.append,
    )
    statuses = [r.status for r in outcome.runs]
    assert (
        statuses.count(RunStatus.SKIPPED) == 2
        and statuses.count(RunStatus.COMPLETED) == 2
        and len(seen) == 2
    )
    exp = service.repo.get_experiment(outcome.experiment_id)
    skipped = [r for r in exp.runs if r.status == "skipped"]
    assert skipped and all(
        r.outcome == "not_verified" and "budget" in r.error_message for r in skipped
    )
    assert exp.variants[0].factors_json == {"b": "solve"}


def test_sweep_cli_end_to_end_and_dashboard(tmp_path: Path, settings: Settings):
    env = _env(tmp_path)
    dry = runner.invoke(app, ["sweep", "run", "demo-fake", "--dry-run"], env=env)
    assert (
        dry.exit_code == 0
        and "behavior=solve|cost=cheap|granularity=batched" in dry.output
        and "8 configuration(s)" in dry.output
    )
    result = runner.invoke(
        app,
        ["sweep", "run", "demo-fake", "--parallelism", "3", "--tasks"]
        if False
        else ["sweep", "run", "demo-fake", "--parallelism", "3"],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "cheapest verified configuration:" in result.output
    assert "behavior=solve|cost=cheap|granularity=batched" in result.output
    match = re.search(r"experiment id: (exp_[a-z0-9]+)", result.output)
    exp_id = match.group(1)
    report = runner.invoke(app, ["sweep", "report", exp_id], env=env)
    assert report.exit_code == 0 and "factor effects" in report.output
    listing = runner.invoke(app, ["sweep", "list"], env=env)
    assert (
        listing.exit_code == 0
        and "demo-fake" in listing.output
        and "claude-config-search" in listing.output
    )
    not_sweep = runner.invoke(
        app,
        ["run", "demo", "--variants", "fake-reference", "--tasks", "fix-month-boundary"],
        env=env,
    )
    plain_id = re.search(r"experiment id: (exp_[a-z0-9]+)", not_sweep.output).group(1)
    assert runner.invoke(app, ["sweep", "report", plain_id], env=env).exit_code == 1
    missing = runner.invoke(app, ["sweep", "run", "nope.yaml"], env=env)
    assert missing.exit_code == 2 and "bundled" in missing.output

    # Dashboard + export read the same recommendation.
    home = Path(env["HARNESSLAB_HOME"])
    hl = Settings(home=home)
    db = Database(hl.resolved_database_url)
    app_ = create_app(hl, db)
    with TestClient(app_) as client:
        page = client.get(f"/experiments/{exp_id}")
        assert (
            page.status_code == 200
            and "Cheapest verified configuration" in page.text
            and "Factor effects" in page.text
        )
        data = client.get(
            f"/api/experiments/{exp_id}/export.json?events=false&artifacts=false"
        ).json()
        assert data["sweep"]["name"] == "demo-fake-sweep" and data["sweep_report"]["workloads"][0][
            "recommended"
        ]["variant_key"].startswith("behavior=solve|cost=cheap")
        assert data["variants"][0]["factors"]
    db.dispose()


def test_sweep_spec_loading_errors(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\nsuite: demo\nbase_variant: {runner: fake}\nfactors: {a: [1]}\nholdout_tasks: [nope]\n"
    )
    with pytest.raises(SpecError, match="holdout_tasks"):
        load_sweep_target(bad)
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "name: x\nsuite: demo\nbase_variant: {runner: fake}\nfactors: {a: [1, 2]}\ntasks: [fix-month-boundary]\n"
    )
    spec, suite, tasks = load_sweep_target(ok)
    assert [t.id for t in tasks] == ["fix-month-boundary"] and len(expand_sweep(spec)) == 2


def test_claude_sweep_knobs_and_codex_prompt_prefix(fake_cli: Path, tmp_path: Path, monkeypatch):
    argv = build_claude_command(
        RunnerConfig(
            runner="claude",
            options={
                "reasoning_effort": "low",
                "autocompact": "100k",
                "action_policy": "batched",
                "append_system_prompt": "Be terse.",
            },
        )
    )
    assert (
        argv[argv.index("--effort") + 1] == "low"
        and argv[argv.index("--autocompact") + 1] == "100k"
    )
    system = argv[argv.index("--append-system-prompt") + 1]
    assert system.startswith("Be terse.") and "batched steps" in system
    free = build_claude_command(
        RunnerConfig(runner="claude", options={"action_policy": "Always run tests first."})
    )
    assert "Always run tests first." in free

    import asyncio

    from harnesslab.core.events import EventEmitter
    from harnesslab.core.models import RepoSpec, TaskSpec, VerificationSpec
    from harnesslab.runners.codex import CodexRunner
    from harnesslab.trace.redaction import Redactor

    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setenv("FAKE_CLI_STREAM", str(FIXTURES / "codex" / "exec_success.jsonl"))
    monkeypatch.setenv("FAKE_CLI_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    task = TaskSpec(
        id="t",
        name="t",
        repo=RepoSpec(path=str(worktree)),
        prompt="Fix it",
        verification=VerificationSpec(command="true"),
    )
    emitter = EventEmitter("r", redactor=Redactor(include_process_env=False))
    config = RunnerConfig(
        runner="codex",
        options={
            "executable": str(fake_cli),
            "action_policy": "fine",
            "env_passthrough": ["FAKE_CLI_STREAM", "FAKE_CLI_PROMPT_OUT"],
        },
    )
    asyncio.run(CodexRunner().run(task, worktree, config, emitter))
    prompt = (tmp_path / "prompt.txt").read_text()
    assert prompt.startswith("Action policy: work in small") and prompt.endswith("Fix it")
    launch = next(e for e in emitter.events if e.name == "harness_launch")
    assert launch.payload["prompt_prefix_hash"]


def test_schema_migration_adds_missing_columns(settings: Settings):
    import sqlite3

    db = Database(settings.resolved_database_url)
    db.create_all()
    db.dispose()
    conn = sqlite3.connect(settings.db_path)
    conn.execute("ALTER TABLE variants DROP COLUMN factors_json")
    conn.commit()
    assert "factors_json" not in [r[1] for r in conn.execute("PRAGMA table_info(variants)")]
    conn.close()
    db = Database(settings.resolved_database_url)
    db.create_all()
    db.dispose()
    conn = sqlite3.connect(settings.db_path)
    assert "factors_json" in [r[1] for r in conn.execute("PRAGMA table_info(variants)")]
    conn.close()
    json.dumps({"ok": True})


def test_sweep_can_minimize_llm_calls():
    spec = _spec(
        objective={"minimize": "llm_calls", "tie_breaker": "llm_calls"},
        repetitions=1,
        factors={"g": ["a", "b"]},
    )
    samples = [
        RunSample(run_id="1", task_key="t", variant_key="g=a", verified_pass=True, llm_calls=9),
        RunSample(run_id="2", task_key="t", variant_key="g=b", verified_pass=True, llm_calls=3),
    ]
    report = analyze_sweep(spec, samples, {"g=a": {"g": "a"}, "g=b": {"g": "b"}}, {"t": []})
    assert report.objective_kind == "llm_calls"
    assert report.workloads[0].recommended.variant_key == "g=b"
    assert report.workloads[0].recommended.median_llm_calls == 3


def test_harness_bundle_as_sweep_factor(tmp_path: Path):
    from harnesslab.experiments.spec import resolve_variant_harnesses

    for name, prompt in (("a", "Be terse."), ("b", "Be thorough.")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "system_prompt.md").write_text(prompt)
    spec = _spec(
        factors={
            "harness": {"a": {"harness": "a"}, "b": {"harness": "b"}},
            "model": ["m-small", "m-big"],
        },
        source_path=tmp_path / "sweep.yaml",
    )
    variants = expand_sweep(spec)
    resolve_variant_harnesses(variants, spec.base_dir)
    assert len(variants) == 4 and all(v.harness_hash for v in variants)
    by_key = {v.id: v for v in variants}
    assert by_key["harness=a|model=m-small"].harness_dir == (tmp_path / "a").resolve()
    assert (
        by_key["harness=a|model=m-small"].harness_hash
        != by_key["harness=b|model=m-small"].harness_hash
    )
    assert (
        by_key["harness=a|model=m-small"].runner_config().config_hash()
        != by_key["harness=b|model=m-small"].runner_config().config_hash()
    )
    assert "harness_dir" not in by_key["harness=a|model=m-big"].runner_config().model_dump(
        mode="json"
    )

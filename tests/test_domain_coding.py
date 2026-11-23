"""The coding domain adapter: the existing run pipeline, unchanged, behind the domain contract.

Every test runs offline (the fake runner, or a stub runner a test registers; no network, no API
keys), each against its own fresh Harness Lab home. Cells are run the way the core runs them:
concurrently in one event loop, with ``verify`` and ``metrics`` called right after ``run`` on the
same attempt.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, Outcome, RunnerResult, RunStatus, SweepSpec
from harnesslab.domains import ensure_builtin_domains
from harnesslab.domains.coding import CHECKER, CodingAdapter, create_adapter
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import (
    SpecError,
    load_suite,
    resolve_suite_target,
    resolve_variant_harnesses,
    resolve_variants,
)
from harnesslab.experiments.sweep import expand_sweep
from harnesslab.runners import base as runners_base
from harnesslab.runners.fake import FakeRunner
from harnesslab.storage.database import Database
from harnesslab.storage.models import RunRow
from harnesslab.storage.repository import Repository
from harnesslab.study.budget import Budget
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapter,
    RunContext,
    Task,
    Verdict,
    VerdictStatus,
    VerifyContext,
)
from harnesslab.study.registry import create_domain
from harnesslab.study.runner import Cell as StudyCell
from harnesslab.study.runner import HarnessError, run_cells
from harnesslab.study.store import StudyStore
from tests.conftest import FIXTURES

DEMO_VARIANTS = ["fake-reference", "fake-noop", "fake-flaky", "fake-partial"]
REPETITIONS = 3
PARALLELISM = 4
FIXTURE_SUITE_DIR = FIXTURES / "coding"

# Run status -> attempt status (anything else is an error) and verifier outcome -> verdict,
# stated here independently of the adapter's own tables.
EXPECTED_STATUS = {
    RunStatus.COMPLETED: AttemptStatus.COMPLETED,
    RunStatus.TIMEOUT: AttemptStatus.TIMEOUT,
    RunStatus.UNAVAILABLE: AttemptStatus.UNAVAILABLE,  # when the harness never ran
}
EXPECTED_VERDICT = {
    Outcome.PASS: VerdictStatus.PASS,
    Outcome.FAIL: VerdictStatus.FAIL,
    Outcome.NOT_VERIFIED: VerdictStatus.UNVERIFIABLE,
}

Cell = tuple[Task, Config, int]
Result = tuple[Attempt, Verdict, dict]


def _contexts(
    root: Path, study: str, phase: str, task: Task, config: Config, repetition: int
) -> tuple[RunContext, VerifyContext]:
    attempt_dir = root / "cells" / phase / task.id / config.config_hash[:12] / str(repetition)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    return (
        RunContext(
            study=study,
            phase=phase,
            repetition=repetition,
            attempt_dir=attempt_dir,
            keep_artifacts=False,
            cost_limit_usd=None,
            seed=repetition,
        ),
        VerifyContext(study=study, phase=phase, attempt_dir=attempt_dir, keep_artifacts=False),
    )


async def _run_cells(
    adapter: CodingAdapter, cells: list[Cell], root: Path, *, study: str, phase: str
) -> dict[tuple[str, str, int], Result]:
    """Run, verify and measure every cell; keyed by (task id, config hash, repetition)."""
    semaphore = asyncio.Semaphore(PARALLELISM)

    async def one(task: Task, config: Config, repetition: int):
        async with semaphore:
            run_ctx, verify_ctx = _contexts(root, study, phase, task, config, repetition)
            attempt = await adapter.run(task, config, run_ctx)
            verdict = await adapter.verify(task, attempt, verify_ctx)
            metrics = adapter.metrics(task, attempt, verdict)
        assert (attempt.task_id, attempt.config_hash, attempt.repetition) == (
            task.id,
            config.config_hash,
            repetition,
        )
        # What the core persists for a cell: one JSON line, without the payload.
        line = json.dumps(
            {
                "attempt": attempt.model_dump(mode="json"),
                "verdict": verdict.model_dump(mode="json"),
                "metrics": metrics,
            },
            allow_nan=False,
        )
        assert "payload" not in json.loads(line)["attempt"]
        return (task.id, config.config_hash, repetition), (attempt, verdict, metrics)

    return dict(await asyncio.gather(*(one(t, c, r) for t, c, r in cells)))


def _open(home: Path) -> tuple[Database, Repository]:
    db = Database(Settings(home=home).resolved_database_url)
    return db, Repository(db, home)


async def test_adapter_reproduces_run_experiment_on_the_demo_suite(tmp_path: Path):
    suite_path = resolve_suite_target("demo")
    suite, tasks = load_suite(suite_path)

    # 1. The existing pipeline, as `harnesslab run demo` runs it.
    pipeline_home = tmp_path / "pipeline-home"
    settings = Settings(home=pipeline_home)
    settings.ensure_dirs()
    db = Database(settings.resolved_database_url)
    db.create_all()
    try:
        experiment = ExperimentSpec(
            name="pipeline",
            suite=str(suite_path),
            repetitions=REPETITIONS,
            parallelism=PARALLELISM,
        )
        service = ExperimentService(settings, db)
        outcome = await service.run_experiment(
            experiment, suite, tasks, resolve_variants(DEMO_VARIANTS, experiment, suite)
        )
        pipeline_exp = service.repo.get_experiment(outcome.experiment_id)
    finally:
        db.dispose()
    pipeline_task_hashes = {t.task_key: t.task_hash for t in pipeline_exp.tasks}
    pipeline_variant_hashes = {v.variant_key: v.config_hash for v in pipeline_exp.variants}
    expected = {(r.task_key, r.variant_key, r.repetition): r for r in outcome.runs}
    assert len(expected) == len(tasks) * len(DEMO_VARIANTS) * REPETITIONS == 36
    # The scenario is not trivial: every run completed, and the verdicts differ by variant,
    # by task (fake-partial) and by repetition (fake-flaky).
    assert {r.status for r in expected.values()} == {RunStatus.COMPLETED}
    for task in tasks:
        for rep in range(REPETITIONS):
            assert expected[(task.id, "fake-reference", rep)].outcome == Outcome.PASS
            assert expected[(task.id, "fake-noop", rep)].outcome == Outcome.FAIL
            flaky = Outcome.PASS if rep == 1 else Outcome.FAIL
            assert expected[(task.id, "fake-flaky", rep)].outcome == flaky
            partial = Outcome.PASS if task.id == "fix-month-boundary" else Outcome.FAIL
            assert expected[(task.id, "fake-partial", rep)].outcome == partial

    # 2. The same cells through the adapter, in a fresh home of its own.
    adapter_home = tmp_path / "adapter-home"
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(adapter_home),
            "knobs": {"variant": "suite"},
            "variants": DEMO_VARIANTS,
            "baseline": {"variant": "fake-noop"},
        },
        tmp_path / "study",
    )
    space = adapter.search_space()
    assert space.names == ["variant"] and space.knob("variant").levels == tuple(DEMO_VARIANTS)
    assert adapter.baseline_config().values == {"variant": "fake-noop"}
    space.validate_config(adapter.baseline_config())
    assert adapter.paid is False

    loaded = adapter.load_tasks([t.id for t in tasks])
    assert [t.id for t in loaded] == [t.id for t in tasks]
    # The content hash is the pipeline's task_hash: spec hash and fixture base commit.
    assert {t.id: t.content_hash for t in loaded} == pipeline_task_hashes
    assert all(t.data["base_commit"] == pipeline_exp.tasks[0].base_commit for t in loaded)

    configs = {v: Config(values={"variant": v}, label=v) for v in DEMO_VARIANTS}
    cells = [(t, configs[v], r) for r in range(REPETITIONS) for t in loaded for v in DEMO_VARIANTS]
    try:
        results = await _run_cells(adapter, cells, tmp_path, study="parity", phase="dev")
    finally:
        await adapter.close()

    assert len(results) == len(expected)
    for (task_id, variant_id, rep), run in expected.items():
        attempt, verdict, metrics = results[(task_id, configs[variant_id].config_hash, rep)]
        cell = f"{task_id} / {variant_id} / {rep}"
        assert attempt.status == EXPECTED_STATUS.get(run.status, AttemptStatus.ERROR), cell
        assert attempt.details["run_status"] == run.status.value, cell
        assert attempt.output["outcome"] == run.outcome.value, cell
        assert attempt.output["verified_score"] == run.metrics.verified_score, cell
        assert verdict.status == EXPECTED_VERDICT[run.outcome], cell
        assert verdict.checker == CHECKER, cell
        assert metrics["verified_score"] == run.metrics.verified_score, cell
        # The fake runner is deterministic, so the run's other metrics agree too.
        for key in ("input_tokens", "output_tokens", "llm_calls", "tool_calls", "files_changed"):
            assert metrics[key] == getattr(run.metrics, key), f"{cell}: {key}"
        assert metrics["reported_cost_usd"] == run.metrics.reported_cost_usd, cell

    # Usage: fake-partial reports a (simulated) cost, the others report none.
    partial, _, partial_metrics = results[
        ("fix-month-boundary", configs["fake-partial"].config_hash, 0)
    ]
    assert partial.usage.cost_usd > 0 and partial.details["cost_source"] == "reported"
    assert partial_metrics["cost_usd"] == partial.usage.cost_usd
    assert partial.usage.input_tokens == partial_metrics["input_tokens"] > 0
    assert partial.usage.llm_calls == partial_metrics["llm_calls"] > 0
    reference, _, reference_metrics = results[
        ("fix-month-boundary", configs["fake-reference"].config_hash, 0)
    ]
    assert reference.usage.cost_usd == 0.0 and reference.details["cost_source"] == "none"
    assert reference_metrics["cost_usd"] is None and reference_metrics["files_changed"] > 0

    # The adapter's own Harness Lab records: one experiment for (study, phase), finished.
    db, repo = _open(adapter_home)
    try:
        [summary] = repo.list_experiments()
        assert summary["name"] == "study:parity:dev" and summary["status"] == "completed"
        assert summary["runs"] == len(expected)
        exp = repo.get_experiment(summary["id"])
        assert {t.task_key: t.task_hash for t in exp.tasks} == pipeline_task_hashes
        assert {v.variant_key: v.config_hash for v in exp.variants} == pipeline_variant_hashes
        assert {r.id for r in exp.runs} == {a.output["run_id"] for a, _, _ in results.values()}
        assert {a.output["experiment_id"] for a, _, _ in results.values()} == {exp.id}
        assert all(r.finished_at is not None and r.worktree_path for r in exp.runs)
        assert not any(Path(r.worktree_path).exists() for r in exp.runs)
    finally:
        db.dispose()


async def test_sweep_factor_knobs_build_the_variants_expand_sweep_builds(tmp_path: Path):
    study_dir = tmp_path / "study"
    bundle = study_dir / "bundles" / "terse"
    bundle.mkdir(parents=True)
    (bundle / "system_prompt.md").write_text("Be terse.\n")
    base_variant = {"runner": "fake", "delay_ms": 0}
    knobs = {
        "behavior": ["solve", "noop"],
        "simulate_cost_usd_per_1k_tokens": [0.001, 0.01],
        "harness": {"none": {}, "terse": {"harness": "bundles/terse"}},
    }
    baseline = {"behavior": "noop", "simulate_cost_usd_per_1k_tokens": 0.001, "harness": "none"}
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": "hl-home",  # relative: inside the study directory
            "base_variant": base_variant,
            "knobs": knobs,
            "baseline": baseline,
        },
        study_dir,
    )
    assert adapter.settings.home == (study_dir / "hl-home").resolve()
    space = adapter.search_space()
    assert space.names == list(knobs) and space.size == 8
    assert space.knob("behavior").levels == ("solve", "noop")
    assert space.knob("simulate_cost_usd_per_1k_tokens").levels == (0.001, 0.01)
    assert space.knob("harness").levels == ("none", "terse")
    assert adapter.baseline_config().values == baseline
    assert adapter.paid is False and adapter.runners == ["fake"]

    [task] = adapter.load_tasks(["fix-month-boundary"])
    configs = [
        Config(values={"behavior": b, "simulate_cost_usd_per_1k_tokens": c, "harness": h})
        for b in ("solve", "noop")
        for c in (0.001, 0.01)
        for h in ("none", "terse")
    ]
    try:
        smoke = await _run_cells(
            adapter, [(task, adapter.baseline_config(), 0)], tmp_path, study="fx", phase="smoke"
        )
        dev = await _run_cells(
            adapter, [(task, c, 0) for c in configs], tmp_path, study="fx", phase="dev"
        )
    finally:
        await adapter.close()

    [(smoke_attempt, smoke_verdict, _)] = smoke.values()
    assert smoke_verdict.status == VerdictStatus.FAIL
    assert smoke_attempt.details["variant"] == (
        "behavior=noop|simulate_cost_usd_per_1k_tokens=0.001|harness=none"
    )
    for config in configs:
        attempt, verdict, metrics = dev[(task.id, config.config_hash, 0)]
        solved = config.values["behavior"] == "solve"
        assert verdict.status == (VerdictStatus.PASS if solved else VerdictStatus.FAIL)
        assert attempt.status == AttemptStatus.COMPLETED
        assert attempt.details["cost_source"] == "reported" and attempt.usage.cost_usd > 0
        assert metrics["cost_usd"] == attempt.usage.cost_usd
        assert (attempt.details["harness_hash"] is not None) == (
            config.values["harness"] == "terse"
        )
        if config.values["simulate_cost_usd_per_1k_tokens"] == 0.01:
            cheap = config.values | {"simulate_cost_usd_per_1k_tokens": 0.001}
            cheap_attempt = dev[(task.id, Config(values=cheap).config_hash, 0)][0]
            assert attempt.usage.cost_usd == pytest.approx(10 * cheap_attempt.usage.cost_usd)

    # Each configuration ran exactly the variant `harnesslab sweep` builds for those levels.
    grid = expand_sweep(
        SweepSpec(name="grid", suite="demo", base_variant=base_variant, factors=knobs)
    )
    resolve_variant_harnesses(grid, study_dir)
    expected = {v.id: (v.factors, v.runner_config().config_hash(), v.harness_hash) for v in grid}
    db, repo = _open(adapter.settings.home)
    try:
        by_name = {e["name"]: e for e in repo.list_experiments()}
        assert set(by_name) == {"study:fx:smoke", "study:fx:dev"}
        assert {e["status"] for e in by_name.values()} == {"completed"}
        assert by_name["study:fx:smoke"]["runs"] == 1 and by_name["study:fx:dev"]["runs"] == 8
        exp = repo.get_experiment(by_name["study:fx:dev"]["id"])
        got = {v.variant_key: (v.factors_json, v.config_hash, v.harness_hash) for v in exp.variants}
        assert got == expected
        for row in exp.variants:
            if row.harness_hash is None:
                assert row.harness_json is None
                continue
            assert [f["path"] for f in row.harness_json["files"]] == ["system_prompt.md"]
            snapshot = adapter.settings.artifacts_dir / exp.id / "harness" / row.harness_hash
            assert (snapshot / "system_prompt.md").read_text() == "Be terse.\n"
    finally:
        db.dispose()


async def test_crashed_and_unavailable_runs(tmp_path: Path):
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "base_variant": {"runner": "fake"},
            "knobs": {
                "agent": {
                    "fake-crash": {"behavior": "crash"},
                    # A missing CLI: found unavailable before anything runs, so nothing is spent.
                    "missing-cli": {"runner": "codex", "executable": "codex-does-not-exist-xyz"},
                }
            },
            "baseline": {"agent": "fake-crash"},
        },
        tmp_path / "study",
    )
    assert adapter.paid is True and adapter.runners == ["codex", "fake"]
    [task] = adapter.load_tasks(["fix-month-boundary"])
    crash = Config(values={"agent": "fake-crash"})
    missing = Config(values={"agent": "missing-cli"})
    try:
        results = await _run_cells(
            adapter, [(task, crash, 0), (task, missing, 0)], tmp_path, study="st", phase="dev"
        )
    finally:
        await adapter.close()

    attempt, verdict, _ = results[(task.id, crash.config_hash, 0)]
    assert attempt.status == AttemptStatus.ERROR and attempt.details["run_status"] == "crashed"
    assert "simulated adapter crash" in attempt.error
    # The hidden tests still ran on the unchanged worktree, and failed.
    assert verdict.status == VerdictStatus.FAIL and attempt.output["outcome"] == "fail"

    attempt, verdict, metrics = results[(task.id, missing.config_hash, 0)]
    assert attempt.status == AttemptStatus.UNAVAILABLE
    assert attempt.output["outcome"] == "not_verified" and attempt.output["verified_score"] is None
    assert verdict.status == VerdictStatus.UNVERIFIABLE and not verdict.passed
    assert "unavailable" in verdict.reason
    assert attempt.usage.cost_usd == 0.0 and metrics["cost_usd"] is None


class _StatusRunner(runners_base.HarnessRunner):
    """Returns at once with the run status its ``status`` option names, changing nothing."""

    name = "test-coding-status"

    async def run(self, task, worktree, config, emit) -> RunnerResult:
        status = RunStatus(config.get("status"))
        return RunnerResult(status=status, error=f"stub runner ended {status.value}")


async def test_timeout_and_other_run_statuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(runners_base._REGISTRY, _StatusRunner.name, _StatusRunner)
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "base_variant": {"runner": _StatusRunner.name},
            "knobs": {"status": ["completed", "timeout", "blocked"]},
            "baseline": {"status": "completed"},
        },
        tmp_path / "study",
    )
    assert adapter.paid is True  # any runner but the fake one may use a paid API
    [task] = adapter.load_tasks(["fix-month-boundary"])
    configs = {s: Config(values={"status": s}) for s in ("completed", "timeout", "blocked")}
    try:
        results = await _run_cells(
            adapter, [(task, c, 0) for c in configs.values()], tmp_path, study="st", phase="dev"
        )
    finally:
        await adapter.close()
    expected = {
        "completed": AttemptStatus.COMPLETED,
        "timeout": AttemptStatus.TIMEOUT,
        "blocked": AttemptStatus.ERROR,
    }
    for status, attempt_status in expected.items():
        attempt, verdict, _ = results[(task.id, configs[status].config_hash, 0)]
        assert attempt.status == attempt_status and attempt.details["run_status"] == status
        # The agent changed nothing, and the hidden tests ran anyway: a failed verdict.
        assert verdict.status == VerdictStatus.FAIL and attempt.output["outcome"] == "fail"
    timed_out = results[(task.id, configs["timeout"].config_hash, 0)][1]
    assert "run timeout: stub runner ended timeout" in timed_out.reason


class _UnavailableFromRound(FakeRunner):
    """The fake improver, whose CLI cannot launch from the round its ``unavailable_from`` option
    names (from round 1: it never runs)."""

    name = "test-coding-unavailable-from"

    async def run(self, task, worktree, config, emit) -> RunnerResult:
        if (config.improve_round or 1) >= int(config.get("unavailable_from")):
            return RunnerResult(status=RunStatus.UNAVAILABLE, error="claude: command not found")
        return await super().run(task, worktree, config, emit)


async def test_an_improvement_run_unavailable_after_it_worked_keeps_its_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Review of the core fixes (F7): an improvement run whose harness worked in round 1 and could
    # not launch in round 2 is reported unavailable by the pipeline, which verified it (Harness
    # Lab counts it). As an unavailable attempt, the study core threw that verdict away, stopped
    # the phase and ran the cell (here a held-out cell) again.
    monkeypatch.setitem(runners_base._REGISTRY, _UnavailableFromRound.name, _UnavailableFromRound)
    adapter = create_adapter(
        {
            "suite": "demo-improve",
            "home": str(tmp_path / "home"),
            "base_variant": {"runner": _UnavailableFromRound.name, "behavior": "solve"},
            "knobs": {"unavailable_from": [2, 1]},
            "baseline": {"unavailable_from": 2},
        },
        tmp_path / "study",
    )
    [task] = adapter.load_tasks(["fewer-key-operations"])
    later, never = (Config(values={"unavailable_from": n}) for n in (2, 1))
    store = StudyStore(tmp_path / "study")

    async def run_cell(config: Config):
        return await run_cells(
            adapter,
            store,
            study="st",
            phase="held_out",
            cells=[StudyCell(task=task, config=config, repetition=0, stage="held_out")],
            budget=Budget(),
            parallelism=1,
            keep_artifacts=False,
        )

    try:
        with store.locked():
            summary = await run_cell(later)
            with pytest.raises(HarnessError, match="unavailable"):
                await run_cell(never)
    finally:
        await adapter.close()
    assert summary.ran == 1 and summary.harness_errors == 0 and summary.verdicts == {"pass": 1}
    worked, never_ran = store.records("held_out")
    assert worked["attempt"]["status"] == "error"
    assert worked["attempt"]["details"]["run_status"] == "unavailable"
    assert worked["harness_error"] is None and worked["verdict"]["status"] == "pass"
    assert worked["verdict"]["checker"] == CHECKER
    assert worked["attempt"]["output"]["outcome"] == "pass"
    # a harness that never ran stays no outcome: the cell runs again
    assert never_ran["attempt"]["status"] == "unavailable" and never_ran["verdict"] is None
    assert never_ran["harness_error"].startswith("unavailable: ")
    assert store.completed("held_out") == {(task.id, later.config_hash, 0)}


def test_one_adapter_serves_several_event_loops(tmp_path: Path):
    """``harnesslab study`` closes an adapter in an event loop of its own, and a program may run
    several phases with one adapter, each under its own ``asyncio.run``."""
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "knobs": {"variant": "suite"},
            "variants": ["fake-reference", "fake-noop"],
            "baseline": {"variant": "fake-noop"},
        },
        tmp_path / "study",
    )
    [task] = adapter.load_tasks(["fix-month-boundary"])
    configs = [Config(values={"variant": v}) for v in ("fake-reference", "fake-noop")]
    # Four cells at once on one fixture repository: their worktrees contend for its lock (an
    # asyncio lock, which belongs to the loop that first waits on it).
    cells = [(task, c, r) for c in configs for r in range(2)]

    async def first_loop() -> dict[tuple[str, str, int], Result]:
        # One cell first, so the four start together once the database is open.
        await _run_cells(adapter, cells[:1], tmp_path / "w", study="loops", phase="smoke")
        return await _run_cells(adapter, cells, tmp_path / "a", study="loops", phase="dev")

    first = asyncio.run(first_loop())
    second = asyncio.run(_run_cells(adapter, cells, tmp_path / "b", study="loops", phase="dev"))
    asyncio.run(adapter.close())
    for results in (first, second):
        got = {key: (a.status, v.status) for key, (a, v, _) in results.items()}
        assert got == {
            (task.id, c.config_hash, r): (
                AttemptStatus.COMPLETED,
                VerdictStatus.PASS
                if c.values["variant"] == "fake-reference"
                else VerdictStatus.FAIL,
            )
            for c in configs
            for r in range(2)
        }
    db, repo = _open(adapter.settings.home)
    try:
        runs = {e["name"]: (e["status"], e["runs"]) for e in repo.list_experiments()}
        assert runs == {
            "study:loops:smoke": ("completed", 1),
            "study:loops:dev": ("completed", 8),
        }
    finally:
        db.dispose()


async def test_suite_without_variants_uses_built_ins_and_suite_defaults(tmp_path: Path):
    # Prices for the fake runner's model: the runs report no cost, so the cost is estimated.
    pricing = tmp_path / "prices.yaml"
    pricing.write_text(
        "version: test-prices\nmodels:\n"
        "  fake-model-v1: {input_per_million: 1000, output_per_million: 2000}\n"
    )
    options = {
        "suite": "suite.yaml",  # relative to the study directory
        "home": str(tmp_path / "home"),
        "pricing": str(pricing),
        "knobs": {"variant": "suite"},
        "baseline": {"variant": "fake-noop"},
    }
    with pytest.raises(ValueError, match="defines no variants"):
        create_adapter(options, FIXTURE_SUITE_DIR)
    adapter = create_adapter(
        {**options, "variants": ["fake-reference", "fake-noop"]}, FIXTURE_SUITE_DIR
    )
    assert adapter.suite.name == "coding-fixture"
    assert adapter.search_space().knob("variant").levels == ("fake-reference", "fake-noop")
    assert adapter.provenance()["pricing_version"] == "test-prices"
    [task] = adapter.load_tasks(["fix-add"])
    assert task.tags == ("python", "bugfix") and task.data["suite"] == "coding-fixture"
    solve = Config(values={"variant": "fake-reference"})
    noop = Config(values={"variant": "fake-noop"})
    try:
        results = await _run_cells(
            adapter, [(task, solve, 0), (task, noop, 0)], tmp_path, study="fx", phase="dev"
        )
    finally:
        await adapter.close()
    solved, verdict, metrics = results[(task.id, solve.config_hash, 0)]
    assert verdict.status == VerdictStatus.PASS and metrics["files_changed"] == 1
    assert solved.details["cost_source"] == "estimated" and metrics["reported_cost_usd"] is None
    assert solved.usage.cost_usd == metrics["cost_usd"] == metrics["estimated_cost_usd"] > 0
    unchanged, verdict, _ = results[(task.id, noop.config_hash, 0)]
    assert verdict.status == VerdictStatus.FAIL
    # The suite's default for the fake runner (llm_calls: 7) reached both runs.
    assert solved.usage.llm_calls == unchanged.usage.llm_calls == 7
    db, repo = _open(adapter.settings.home)
    try:
        [summary] = repo.list_experiments()
        exp = repo.get_experiment(summary["id"])
        assert {v.config_json["options"]["llm_calls"] for v in exp.variants} == {7}
    finally:
        db.dispose()


async def test_a_cancelled_run_marks_its_experiment_interrupted(tmp_path: Path):
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "base_variant": {"runner": "fake"},
            "knobs": {"behavior": ["solve", "timeout"]},
            "baseline": {"behavior": "solve"},
        },
        tmp_path / "study",
    )
    [task] = adapter.load_tasks(["fix-month-boundary"])
    hanging = Config(values={"behavior": "timeout"})  # sleeps far beyond any test
    run_ctx, _ = _contexts(tmp_path, "cancel", "dev", task, hanging, 0)
    pending = asyncio.create_task(adapter.run(task, hanging, run_ctx))
    probe = Database(adapter.settings.resolved_database_url)
    try:
        # Wait until the run has its worktree: the agent is then running (and hanging).
        for _ in range(600):
            await asyncio.sleep(0.05)
            assert not pending.done()
            try:
                with probe.session() as s:
                    path = s.execute(select(RunRow.worktree_path)).scalar()
            except OperationalError:  # the adapter has not created the tables yet
                path = None
            if path:
                break
        else:
            pytest.fail("the run never started")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await adapter.close()
        repo = Repository(probe, adapter.settings.home)
        [summary] = repo.list_experiments()
        assert summary["name"] == "study:cancel:dev" and summary["status"] == "interrupted"
        [run] = repo.get_experiment(summary["id"]).runs
        assert run.status == "interrupted" and not Path(run.worktree_path).exists()
    finally:
        probe.dispose()


async def test_run_verify_and_metrics_refuse_what_they_cannot_map(tmp_path: Path):
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "base_variant": {"runner": "fake"},
            "knobs": {"behavior": ["solve", "noop"]},
            "baseline": {"behavior": "noop"},
        },
        tmp_path / "study",
    )
    [task] = adapter.load_tasks(["fix-month-boundary"])
    run_ctx, verify_ctx = _contexts(tmp_path, "st", "dev", task, adapter.baseline_config(), 0)
    with pytest.raises(ValueError, match="not a level"):
        await adapter.run(task, Config(values={"behavior": "partial"}), run_ctx)
    with pytest.raises(ValueError, match="missing knobs"):
        await adapter.run(task, Config(values={}), run_ctx)
    with pytest.raises(ValueError, match="not in suite"):
        await adapter.run(Task(id="nope", content_hash="x"), adapter.baseline_config(), run_ctx)
    changed = Task(id=task.id, content_hash="0" * 16, data=task.data, tags=task.tags)
    with pytest.raises(ValueError, match="does not match its fixture snapshot"):
        await adapter.run(changed, adapter.baseline_config(), run_ctx)
    with pytest.raises(ValueError, match="unknown task id"):
        adapter.load_tasks(["fix-month-boundary", "nope"])
    # verify and metrics need the run's outcome in the attempt's payload (same process).
    stored = Attempt(
        task_id=task.id,
        config_hash=adapter.baseline_config().config_hash,
        repetition=0,
        status=AttemptStatus.COMPLETED,
        output={"run_id": "run_x"},
    )
    with pytest.raises(TypeError, match="payload"):
        await adapter.verify(task, stored, verify_ctx)
    with pytest.raises(TypeError, match="payload"):
        adapter.metrics(task, stored, Verdict(status=VerdictStatus.PASS, checker=CHECKER))
    await adapter.close()
    # Refused before any run started: the database is opened only by a run that starts.
    assert not adapter.settings.db_path.exists()


def test_options_are_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    good = {
        "suite": "demo",
        "home": str(tmp_path / "home"),
        "knobs": {"variant": "suite"},
        "variants": ["fake-reference", "fake-noop"],
        "baseline": {"variant": "fake-noop"},
    }

    def make(**changes):
        options = {k: v for k, v in {**good, **changes}.items() if v is not None}
        return create_adapter(options, tmp_path / "study")

    assert make().paid is False and make().knob_form == "variant"
    # Without the home option, the home resolves as for every harnesslab command.
    monkeypatch.setenv("HARNESSLAB_HOME", str(tmp_path / "env-home"))
    assert make(home=None).settings.home == (tmp_path / "env-home").resolve()
    # Every variant of the demo suite: the Codex and Claude Code ones may spend money.
    everything = make(variants=None)
    assert everything.search_space().knob("variant").levels == (
        "fake-reference",
        "fake-noop",
        "fake-flaky",
        "fake-partial",
        "codex-default",
        "claude-default",
    )
    assert everything.paid is True and everything.runners == ["claude", "codex", "fake"]

    with pytest.raises(ValueError, match="unknown coding adapter option"):
        make(parallelism=2)
    with pytest.raises(ValueError, match="suite is required"):
        make(suite=None)
    with pytest.raises(SpecError, match="suite not found"):
        make(suite="no-such-suite")
    with pytest.raises(ValueError, match="knobs is required"):
        make(knobs=None)
    with pytest.raises(ValueError, match="cannot be combined"):
        make(knobs={"variant": "suite", "model": ["a"]})
    with pytest.raises(SpecError, match="unknown variant"):
        make(variants=["fake-reference", "no-such-variant"])
    with pytest.raises(ValueError, match="more than once"):
        make(variants=["fake-noop", "fake-noop"])
    with pytest.raises(ValueError, match="non-empty list"):
        make(variants=[])
    with pytest.raises(ValueError, match="baseline is required"):
        make(baseline=None)
    with pytest.raises(ValueError, match="not a level"):
        make(baseline={"variant": "claude-default"})
    with pytest.raises(ValueError, match="base_variant is used only"):
        make(base_variant={"runner": "fake"})

    factors = {
        "variants": None,
        "base_variant": {"runner": "fake"},
        "knobs": {"model": ["a", "b"]},
        "baseline": {"model": "a"},
    }
    assert make(**factors).knob_form == "factors"
    with pytest.raises(ValueError, match="variants is used only"):
        make(**{**factors, "variants": ["fake-noop"]})
    with pytest.raises(ValueError, match="need base_variant"):
        make(**{**factors, "base_variant": None})
    with pytest.raises(ValueError, match="must name a runner"):
        make(**{**factors, "base_variant": {"model": "a"}})
    with pytest.raises(ValueError, match="missing knobs"):
        make(**{**factors, "knobs": {"model": ["a"], "max_turns": [5, 10]}})
    with pytest.raises(ValueError, match="would name the variant"):
        make(**{**factors, "knobs": {"model": [1, "1"]}, "baseline": {"model": 1}})
    with pytest.raises(SpecError, match="harness bundle not found"):
        make(
            **{
                **factors,
                "knobs": {"harness": {"none": {}, "gone": {"harness": "no-such-bundle"}}},
                "baseline": {"harness": "none"},
            }
        )
    # A runner set by any level makes the adapter paid.
    paid = make(
        **{**factors, "knobs": {"runner": ["fake", "claude"]}, "baseline": {"runner": "fake"}}
    )
    assert paid.paid is True and paid.runners == ["claude", "fake"]


def test_the_registry_creates_the_coding_adapter(tmp_path: Path):
    ensure_builtin_domains()
    adapter = create_domain(
        "coding",
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "knobs": {"variant": "suite"},
            "variants": ["fake-reference"],
            "baseline": {"variant": "fake-reference"},
        },
        tmp_path,
    )
    assert isinstance(adapter, CodingAdapter) and isinstance(adapter, DomainAdapter)
    assert adapter.name == "coding" and adapter.version
    provenance = adapter.provenance()
    assert provenance["suite"] == "demo" and provenance["knob_form"] == "variant"
    assert len(provenance["suite_sha256"]) == 64 and provenance["runners"] == ["fake"]
    json.dumps(provenance)


def test_audit_split_flags_shared_prompts_and_fault_twins(tmp_path: Path):
    adapter = create_adapter(
        {
            "suite": "demo",
            "home": str(tmp_path / "home"),
            "knobs": {"variant": "suite"},
            "variants": ["fake-reference"],
            "baseline": {"variant": "fake-reference"},
        },
        tmp_path,
    )
    dev = [Task(id="fix-month-boundary", content_hash="h")]
    assert adapter.audit_split(dev, ["add-tag-budgets", "consolidate-money-formatting"]) == []
    assert adapter.audit_split(dev, ["no-such-task"]) == [
        "held-out task(s) not in suite demo: no-such-task"
    ]
    # A held-out task that is the faulty twin of a dev task, and one that repeats its prompt.
    twin = adapter._tasks["add-tag-budgets"].model_copy(update={"fault_of": "fix-month-boundary"})
    copy = adapter._tasks["consolidate-money-formatting"].model_copy(
        update={"prompt": adapter._tasks["fix-month-boundary"].prompt}
    )
    adapter._tasks.update({"add-tag-budgets": twin, "consolidate-money-formatting": copy})
    problems = adapter.audit_split(dev, ["add-tag-budgets", "consolidate-money-formatting"])
    assert problems == [
        "held-out task add-tag-budgets is the faulty twin of dev task fix-month-boundary",
        "held-out task consolidate-money-formatting has the prompt of dev task fix-month-boundary",
    ]

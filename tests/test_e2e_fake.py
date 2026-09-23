"""End to end: fixture repo -> worktree -> fake agent -> verifier -> DB -> aggregates."""

import json
from pathlib import Path

from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, Outcome, RunStatus, VariantSpec
from harnesslab.experiments.aggregate import aggregate_variants, samples_from_rows
from harnesslab.experiments.export import export_experiment
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite, resolve_variants
from harnesslab.storage.database import Database
from tests.conftest import DEMO_SUITE


async def test_demo_suite_with_fake_variants(settings: Settings, db: Database):
    suite, tasks = load_suite(DEMO_SUITE)
    experiment = ExperimentSpec(
        name="e2e", suite=str(DEMO_SUITE), parallelism=3, source_path=DEMO_SUITE
    )
    variants = resolve_variants(["fake-reference", "fake-noop"], experiment, suite) + [
        VariantSpec(id="fake-crash", runner="fake", behavior="crash"),
        VariantSpec(id="fake-broken", runner="fake", behavior="fail"),
        VariantSpec(id="missing-cli", runner="codex", executable="codex-does-not-exist-xyz"),
    ]
    service = ExperimentService(settings, db)
    progress = []
    outcome = await service.run_experiment(
        experiment, suite, tasks, variants, progress=progress.append
    )
    assert len(outcome.runs) == 15 and len([p for p in progress if p.phase == "finished"]) == 15

    by = {(r.task_key, r.variant_key): r for r in outcome.runs}
    for task in tasks:
        assert (
            by[(task.id, "fake-reference")].outcome == Outcome.PASS
            and by[(task.id, "fake-reference")].metrics.verified_pass is True
        )
        assert (
            by[(task.id, "fake-noop")].outcome == Outcome.FAIL
            and by[(task.id, "fake-noop")].metrics.files_changed == 0
        )
        assert (
            by[(task.id, "fake-broken")].outcome == Outcome.FAIL
            and by[(task.id, "fake-broken")].metrics.files_changed == 1
        )
        crash = by[(task.id, "fake-crash")]
        assert (
            crash.status == RunStatus.CRASHED
            and crash.outcome == Outcome.FAIL
            and "simulated adapter crash" in crash.error
        )
        missing = by[(task.id, "missing-cli")]
        assert missing.status == RunStatus.UNAVAILABLE and missing.outcome == Outcome.NOT_VERIFIED
    budgets = by[("add-tag-budgets", "fake-reference")]
    assert budgets.metrics.verified_score == 1.0
    assert by[("add-tag-budgets", "fake-noop")].metrics.verified_score == 0.0

    # Every run had its own worktree and all of them were removed afterwards.
    exp = service.repo.get_experiment(outcome.experiment_id)
    paths = [r.worktree_path for r in exp.runs if r.worktree_path]
    assert len(paths) == 12 and len(set(paths)) == 12 and not any(Path(p).exists() for p in paths)
    assert all(r.base_commit for r in exp.runs if r.status != "unavailable")
    assert len({t.base_commit for t in exp.tasks}) == 1 and all(t.task_hash for t in exp.tasks)

    # Persisted state: events, verifier results, artifacts.
    run = service.repo.get_run(by[("fix-month-boundary", "fake-reference")].run_id)
    kinds = [e.kind for e in run.events]
    assert (
        kinds[0] == "run_started"
        and kinds[-1] == "run_finished"
        and "file_change" in kinds
        and "command_finished" in kinds
    )
    assert (
        run.verifier_result.passed is True
        and "test_hidden_month_boundary" in run.verifier_result.stderr
    )
    artifact_kinds = {a.kind for a in run.artifacts}
    assert {
        "agent_diff",
        "git_status",
        "diff_stat",
        "prompt",
        "verifier_stdout",
        "verifier_stderr",
    } <= artifact_kinds
    diff = service.repo.read_artifact(next(a for a in run.artifacts if a.kind == "agent_diff"))
    assert "+        return [e for e in self._entries if start <= e.date <= end]" in diff
    assert (
        run.metrics_json["lines_added"] == 1
        and run.metrics_json["lines_deleted"] == 1
        and run.metrics_json["tool_calls"] == 3
    )

    samples = samples_from_rows(
        exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
    )
    aggs = aggregate_variants(samples, [v.id for v in variants])
    assert aggs["fake-reference"].success_rate == 1.0 and aggs["fake-noop"].success_rate == 0.0
    assert (
        aggs["missing-cli"].n_valid == 0
        and aggs["missing-cli"].n_infra_failures == 3
        and aggs["fake-crash"].n_infra_failures == 3
    )

    export = export_experiment(service.repo, outcome.experiment_id)
    assert export["experiment"]["status"] == "completed" and len(export["runs"]) == 15
    assert (
        export["runs"][0]["reproducibility"]["base_commit"]
        and export["aggregates"]["fake-reference"]["n_passed"] == 3
    )
    json.dumps(export)  # must be serialisable without NaN/objects


async def test_keep_worktrees_timeout_and_repetitions(settings: Settings, db: Database):
    suite, tasks = load_suite(DEMO_SUITE)
    task = tasks[0].model_copy(deep=True)
    task.limits.agent_timeout_seconds = 1
    experiment = ExperimentSpec(
        name="kept",
        suite=str(DEMO_SUITE),
        repetitions=2,
        parallelism=2,
        keep_worktrees=True,
        source_path=DEMO_SUITE,
    )
    variants = [
        VariantSpec(id="ref", runner="fake", behavior="solve"),
        VariantSpec(id="slow", runner="fake", behavior="timeout"),
    ]
    service = ExperimentService(settings, db)
    import harnesslab.experiments.service as svc

    original = svc.AGENT_TIMEOUT_GRACE_SECONDS
    svc.AGENT_TIMEOUT_GRACE_SECONDS = 0.5
    try:
        outcome = await service.run_experiment(experiment, suite, [task], variants)
    finally:
        svc.AGENT_TIMEOUT_GRACE_SECONDS = original
    assert len(outcome.runs) == 4
    slow = [r for r in outcome.runs if r.variant_key == "slow"]
    assert all(r.status == RunStatus.TIMEOUT for r in slow) and all(
        r.outcome == Outcome.FAIL for r in slow
    )
    kept = [r for r in outcome.runs if r.variant_key == "ref"]
    assert all(r.worktree_path and Path(r.worktree_path).exists() for r in kept) and {
        r.repetition for r in kept
    } == {0, 1}
    exp = service.repo.get_experiment(outcome.experiment_id)
    assert all(r.worktree_kept for r in exp.runs)


async def test_fake_runner_honours_bundle_fake_yaml(settings: Settings, db: Database, tmp_path):
    from harnesslab.experiments.spec import resolve_variant_harness

    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "fake.yaml").write_text(
        "solve_tasks: [fix-month-boundary]\nfail_tasks: [add-tag-budgets]\nllm_calls: 7\n"
    )
    suite, tasks = load_suite(DEMO_SUITE)
    variant = VariantSpec(id="grown", runner="fake", behavior="noop", harness=str(bundle))
    resolve_variant_harness(variant, tmp_path)
    outcome = await ExperimentService(settings, db).run_experiment(
        ExperimentSpec(name="b", suite=str(DEMO_SUITE), parallelism=3, source_path=DEMO_SUITE),
        suite,
        tasks,
        [variant],
    )
    by = {r.task_key: r for r in outcome.runs}
    assert by["fix-month-boundary"].outcome == Outcome.PASS
    assert by["add-tag-budgets"].outcome == Outcome.FAIL
    assert by["add-tag-budgets"].metrics.files_changed == 1
    assert by["consolidate-money-formatting"].metrics.files_changed == 0
    assert all(r.metrics.llm_calls == 7 for r in outcome.runs)

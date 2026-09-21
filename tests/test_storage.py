from pathlib import Path

from harnesslab.config import Settings
from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import (
    EnvironmentSpec,
    ExperimentSpec,
    Outcome,
    RepoSpec,
    RunMetrics,
    RunnerResult,
    RunStatus,
    SuiteSpec,
    TaskSpec,
    VariantSpec,
    VerificationSpec,
    VerifierResult,
)
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository
from harnesslab.trace.redaction import Redactor


def _populate(repo: Repository, artifacts_dir: Path) -> tuple[str, str]:
    env = EnvironmentSpec.detect()
    exp_id = repo.create_experiment(
        ExperimentSpec(name="exp", suite="s.yaml"),
        SuiteSpec(name="suite"),
        environment=env,
        harnesslab_version="0.1.0",
        harnesslab_commit="abc",
    )
    variant = VariantSpec(
        id="v1", runner="fake", model="m", behavior="solve", context_policy={"window": "small"}
    )
    vid = repo.add_variant(exp_id, variant, 0)
    task = TaskSpec(
        id="t1",
        name="Task 1",
        repo=RepoSpec(path="."),
        prompt="do it",
        verification=VerificationSpec(command="true"),
    )
    tid = repo.add_task(exp_id, task, base_commit="deadbeef", task_hash="h1", position=0)
    config = variant.runner_config()
    run_id = repo.create_run(
        exp_id=exp_id,
        variant_row_id=vid,
        task_row_id=tid,
        repetition=0,
        config=config,
        environment=env,
        task_hash="h1",
        prompt_hash="p1",
        base_commit="deadbeef",
        harnesslab_commit="abc",
        harnesslab_version="0.1.0",
        parser_version="1",
        metrics_version="1",
    )
    emitter = EventEmitter(
        run_id,
        redactor=Redactor(include_process_env=False),
        sink=lambda evs: repo.add_events(run_id, evs),
        flush_threshold=2,
    )
    emitter.emit(EventKind.RUN_STARTED)
    emitter.emit(EventKind.TOOL_STARTED, name="Read", call_id="c", payload={"input": {"path": "x"}})
    emitter.emit(EventKind.TOOL_FINISHED, name="Read", call_id="c", duration_ms=4)
    emitter.flush()
    repo.save_verifier_result(
        run_id,
        VerifierResult(
            passed=True,
            outcome=Outcome.PASS,
            command="true",
            exit_code=0,
            stdout="ok",
            normalized_score=0.9,
            injected_files=["tests/h.py"],
        ),
    )
    diff = artifacts_dir / "agent.diff"
    diff.write_text("diff --git a/x b/x\n+1\n")
    repo.add_artifact(run_id, "agent_diff", diff, "text/x-diff")
    metrics = RunMetrics(
        verified_pass=True,
        verified_score=0.9,
        wall_time_seconds=1.5,
        input_tokens=10,
        output_tokens=2,
        tool_calls=1,
        files_changed=1,
        lines_added=1,
    )
    repo.finalize_run(
        run_id,
        status=RunStatus.COMPLETED,
        outcome="pass",
        runner_result=RunnerResult(
            exit_code=0, model_resolved="m-resolved", cli_version="1.0", provider_session_id="sess"
        ),
        metrics=metrics,
        worktree_path="/wt",
        worktree_kept=False,
        error_message=None,
        events_count=3,
    )
    repo.finish_experiment(exp_id)
    return exp_id, run_id


def test_round_trip(settings: Settings, db: Database, tmp_path: Path):
    repo = Repository(db, settings.home)
    exp_id, run_id = _populate(repo, settings.artifacts_dir)
    exp = repo.get_experiment(exp_id)
    assert (
        exp is not None
        and exp.status == "completed"
        and exp.variants[0].context_policy_json == {"window": "small"}
    )
    assert (
        exp.variants[0].config_json["options"] == {"behavior": "solve"}
        and exp.tasks[0].task_hash == "h1"
    )
    run = repo.get_run(run_id)
    assert (
        run is not None
        and run.status == "completed"
        and run.outcome == "pass"
        and run.verified_score == 0.9
    )
    assert [e.kind for e in run.events] == [
        "run_started",
        "tool_started",
        "tool_finished",
    ] and run.events[1].payload_json == {"input": {"path": "x"}}
    assert run.verifier_result.passed is True and run.verifier_result.injected_files_json == [
        "tests/h.py"
    ]
    assert run.artifacts[0].kind == "agent_diff" and repo.read_artifact(
        run.artifacts[0]
    ).startswith("diff --git")
    assert (
        run.model_resolved == "m-resolved"
        and run.provider_session_id == "sess"
        and run.config_hash == exp.variants[0].config_hash
    )
    listing = repo.list_experiments()
    assert (
        listing[0]["runs"] == 1
        and listing[0]["passed"] == 1
        and listing[0]["best_score"] == 0.9
        and listing[0]["tasks"] == 1
    )
    assert (
        repo.find_experiment(exp_id[:8]).id == exp_id
        and repo.find_experiment("exp").id == exp_id
        and repo.find_run(run_id[:10]).id == run_id
    )
    assert repo.find_experiment("nope") is None and repo.count_experiments() == 1


def test_mark_stale_runs(settings: Settings, db: Database):
    repo = Repository(db, settings.home)
    env = EnvironmentSpec.detect()
    exp_id = repo.create_experiment(
        ExperimentSpec(name="e", suite="s"),
        SuiteSpec(name="s"),
        environment=env,
        harnesslab_version="0",
        harnesslab_commit=None,
    )
    vid = repo.add_variant(exp_id, VariantSpec(id="v", runner="fake"), 0)
    tid = repo.add_task(
        exp_id,
        TaskSpec(
            id="t",
            name="t",
            repo=RepoSpec(path="."),
            prompt="p",
            verification=VerificationSpec(command="true"),
        ),
        base_commit="b",
        task_hash="h",
        position=0,
    )
    run_id = repo.create_run(
        exp_id=exp_id,
        variant_row_id=vid,
        task_row_id=tid,
        repetition=0,
        config=VariantSpec(id="v", runner="fake").runner_config(),
        environment=env,
        task_hash="h",
        prompt_hash="p",
        base_commit=None,
        harnesslab_commit=None,
        harnesslab_version="0",
        parser_version="1",
        metrics_version="1",
    )
    assert repo.mark_stale_runs_interrupted() == 1
    assert (
        repo.get_run(run_id).status == "interrupted"
        and repo.get_experiment(exp_id).status == "interrupted"
    )

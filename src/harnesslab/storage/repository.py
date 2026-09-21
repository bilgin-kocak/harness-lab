"""Persistence API used by the experiment service, CLI and dashboard."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, case, func, select
from sqlalchemy.orm import selectinload

from harnesslab.core.events import Event
from harnesslab.core.ids import new_id
from harnesslab.core.models import (
    EnvironmentSpec,
    ExperimentSpec,
    RunMetrics,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    SuiteSpec,
    TaskSpec,
    VariantSpec,
    VerifierResult,
)
from harnesslab.storage.database import Database
from harnesslab.storage.models import (
    ArtifactRow,
    EventRow,
    ExperimentRow,
    RunRow,
    TaskRow,
    VariantRow,
    VerifierResultRow,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Repository:
    def __init__(self, db: Database, home: Path) -> None:
        self.db = db
        self.home = home

    # -- experiments -------------------------------------------------------
    def create_experiment(
        self,
        spec: ExperimentSpec,
        suite: SuiteSpec,
        *,
        environment: EnvironmentSpec,
        harnesslab_version: str,
        harnesslab_commit: str | None,
    ) -> str:
        exp_id = new_id("exp")
        with self.db.session() as s:
            s.add(
                ExperimentRow(
                    id=exp_id,
                    name=spec.name,
                    description=spec.description,
                    suite_name=suite.name,
                    suite_path=str(suite.source_path) if suite.source_path else None,
                    spec_json=spec.model_dump(mode="json"),
                    status="running",
                    repetitions=spec.repetitions,
                    parallelism=spec.parallelism,
                    created_at=utcnow(),
                    harnesslab_version=harnesslab_version,
                    harnesslab_commit=harnesslab_commit,
                    environment_json=environment.model_dump(mode="json"),
                    environment_hash=environment.environment_hash(),
                )
            )
        return exp_id

    def finish_experiment(self, exp_id: str, status: str = "completed") -> None:
        with self.db.session() as s:
            row = s.get(ExperimentRow, exp_id)
            if row is not None:
                row.status = status
                row.finished_at = utcnow()

    def add_variant(self, exp_id: str, variant: VariantSpec, position: int) -> str:
        vid = new_id("var")
        config = variant.runner_config()
        with self.db.session() as s:
            s.add(
                VariantRow(
                    id=vid,
                    experiment_id=exp_id,
                    variant_key=variant.id,
                    position=position,
                    runner=variant.runner,
                    description=variant.description,
                    model_requested=variant.model,
                    model_provider=variant.model_provider,
                    harness_version=variant.harness_version,
                    skill_version=variant.skill_version,
                    context_policy_json=variant.context_policy,
                    tool_policy_json=variant.tool_policy,
                    config_json=config.model_dump(mode="json"),
                    config_hash=config.config_hash(),
                )
            )
        return vid

    def add_task(
        self, exp_id: str, task: TaskSpec, *, base_commit: str, task_hash: str, position: int
    ) -> str:
        tid = new_id("task")
        with self.db.session() as s:
            s.add(
                TaskRow(
                    id=tid,
                    experiment_id=exp_id,
                    task_key=task.id,
                    position=position,
                    name=task.name,
                    version=task.version,
                    task_hash=task_hash,
                    spec_hash=task.spec_hash(),
                    prompt_hash=task.prompt_hash(),
                    base_commit=base_commit,
                    repo_path=str(task.repo_path),
                    tags_json=list(task.tags),
                    spec_json=task.model_dump(mode="json"),
                )
            )
        return tid

    # -- runs --------------------------------------------------------------
    def create_run(
        self,
        *,
        exp_id: str,
        variant_row_id: str,
        task_row_id: str,
        repetition: int,
        config: RunnerConfig,
        environment: EnvironmentSpec,
        task_hash: str,
        prompt_hash: str,
        base_commit: str | None,
        harnesslab_commit: str | None,
        harnesslab_version: str,
        parser_version: str,
        metrics_version: str,
    ) -> str:
        run_id = new_id("run")
        with self.db.session() as s:
            s.add(
                RunRow(
                    id=run_id,
                    experiment_id=exp_id,
                    variant_id=variant_row_id,
                    task_id=task_row_id,
                    repetition=repetition,
                    status=RunStatus.RUNNING.value,
                    started_at=utcnow(),
                    base_commit=base_commit,
                    harnesslab_commit=harnesslab_commit,
                    harnesslab_version=harnesslab_version,
                    runner=config.runner,
                    runner_config_json=config.model_dump(mode="json"),
                    config_hash=config.config_hash(),
                    environment_json=environment.model_dump(mode="json"),
                    environment_hash=environment.environment_hash(),
                    task_hash=task_hash,
                    prompt_hash=prompt_hash,
                    parser_version=parser_version,
                    metrics_version=metrics_version,
                    model_requested=config.model,
                )
            )
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            row = s.get(RunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            for key, value in fields.items():
                setattr(row, key, value)

    def finalize_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        outcome: str,
        runner_result: RunnerResult,
        metrics: RunMetrics,
        worktree_path: str | None,
        worktree_kept: bool,
        error_message: str | None,
        events_count: int,
    ) -> None:
        with self.db.session() as s:
            row = s.get(RunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            row.status = status.value
            row.outcome = outcome
            row.finished_at = utcnow()
            row.worktree_path = worktree_path
            row.worktree_kept = worktree_kept
            row.model_resolved = runner_result.model_resolved
            row.cli_version = runner_result.cli_version
            row.provider_session_id = runner_result.provider_session_id
            row.agent_exit_code = runner_result.exit_code
            row.error_message = error_message
            row.final_message = runner_result.final_message
            row.runner_metadata_json = runner_result.metadata
            row.metrics_json = metrics.model_dump(mode="json")
            row.verified_pass = metrics.verified_pass
            row.verified_score = metrics.verified_score
            row.wall_time_seconds = metrics.wall_time_seconds
            row.input_tokens = metrics.input_tokens
            row.output_tokens = metrics.output_tokens
            row.cached_input_tokens = metrics.cached_input_tokens
            row.reported_cost_usd = metrics.reported_cost_usd
            row.estimated_cost_usd = metrics.estimated_cost_usd
            row.tool_calls = metrics.tool_calls
            row.shell_commands = metrics.shell_commands
            row.files_changed = metrics.files_changed
            row.lines_added = metrics.lines_added
            row.lines_deleted = metrics.lines_deleted
            row.verifier_exit_code = metrics.verifier_exit_code
            row.events_count = events_count

    def add_events(self, run_id: str, events: list[Event]) -> None:
        if not events:
            return
        with self.db.session() as s:
            s.add_all(
                EventRow(
                    id=e.event_id,
                    run_id=run_id,
                    sequence=e.sequence,
                    timestamp=e.timestamp,
                    kind=e.kind.value,
                    source=e.source,
                    name=e.name,
                    duration_ms=e.duration_ms,
                    call_id=e.call_id,
                    parent_call_id=e.parent_call_id,
                    payload_json=e.payload,
                    raw_metadata_json=e.raw_metadata,
                )
                for e in events
            )

    def save_verifier_result(self, run_id: str, result: VerifierResult) -> None:
        with self.db.session() as s:
            existing = s.execute(
                select(VerifierResultRow).where(VerifierResultRow.run_id == run_id)
            ).scalar_one_or_none()
            if existing is not None:
                s.delete(existing)
                s.flush()
            s.add(
                VerifierResultRow(
                    id=new_id("ver"),
                    run_id=run_id,
                    passed=result.passed,
                    outcome=result.outcome.value,
                    command=result.command,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    duration_ms=result.duration_ms,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    score=result.score,
                    max_score=result.max_score,
                    normalized_score=result.normalized_score,
                    score_metrics_json=result.score_metrics,
                    score_command=result.score_command,
                    score_exit_code=result.score_exit_code,
                    score_error=result.score_error,
                    protected_violations_json=result.protected_violations,
                    injected_files_json=result.injected_files,
                    skipped_reason=result.skipped_reason,
                    verifier_version=result.verifier_version,
                    created_at=utcnow(),
                )
            )

    def add_artifact(
        self, run_id: str, kind: str, path: Path, media_type: str = "text/plain"
    ) -> str:
        aid = new_id("art")
        data = path.read_bytes() if path.exists() else b""
        try:
            rel = str(path.resolve().relative_to(self.home.resolve()))
        except ValueError:
            rel = str(path)
        with self.db.session() as s:
            s.add(
                ArtifactRow(
                    id=aid,
                    run_id=run_id,
                    kind=kind,
                    path=rel,
                    media_type=media_type,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    created_at=utcnow(),
                )
            )
        return aid

    def artifact_path(self, artifact: ArtifactRow) -> Path:
        path = Path(artifact.path)
        return path if path.is_absolute() else self.home / path

    def read_artifact(self, artifact: ArtifactRow, cap: int | None = None) -> str:
        path = self.artifact_path(artifact)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if cap is not None and len(text) > cap:
            return text[:cap] + f"\n... [truncated {len(text) - cap} characters]"
        return text

    def mark_stale_runs_interrupted(self) -> int:
        with self.db.session() as s:
            rows = (
                s.execute(select(RunRow).where(RunRow.status.in_(["running", "pending"])))
                .scalars()
                .all()
            )
            for row in rows:
                row.status = RunStatus.INTERRUPTED.value
                row.finished_at = row.finished_at or utcnow()
                row.error_message = row.error_message or "run was interrupted before it finished"
            exps = (
                s.execute(select(ExperimentRow).where(ExperimentRow.status == "running"))
                .scalars()
                .all()
            )
            for exp in exps:
                exp.status = "interrupted"
                exp.finished_at = exp.finished_at or utcnow()
            return len(rows)

    # -- reads -------------------------------------------------------------
    def list_experiments(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            exps = (
                s.execute(
                    select(ExperimentRow)
                    .options(selectinload(ExperimentRow.variants))
                    .order_by(ExperimentRow.created_at.desc())
                )
                .scalars()
                .all()
            )
            summaries: list[dict[str, Any]] = []
            for exp in exps:
                stats = s.execute(
                    select(
                        func.count(RunRow.id),
                        func.max(RunRow.verified_score),
                        func.count(TaskRow.id.distinct()),
                        func.sum(case((RunRow.verified_pass.is_(True), 1), else_=0), type_=Integer),
                    )
                    .select_from(RunRow)
                    .join(TaskRow, TaskRow.id == RunRow.task_id)
                    .where(RunRow.experiment_id == exp.id)
                ).one()
                summaries.append(
                    {
                        "id": exp.id,
                        "name": exp.name,
                        "created_at": exp.created_at,
                        "finished_at": exp.finished_at,
                        "status": exp.status,
                        "suite_name": exp.suite_name,
                        "variants": [v.variant_key for v in exp.variants],
                        "runs": int(stats[0] or 0),
                        "best_score": stats[1],
                        "tasks": int(stats[2] or 0),
                        "passed": int(stats[3] or 0),
                    }
                )
            return summaries

    def get_experiment(self, exp_id: str) -> ExperimentRow | None:
        with self.db.session() as s:
            return s.execute(
                select(ExperimentRow)
                .where(ExperimentRow.id == exp_id)
                .options(
                    selectinload(ExperimentRow.variants),
                    selectinload(ExperimentRow.tasks),
                    selectinload(ExperimentRow.runs).selectinload(RunRow.verifier_result),
                )
            ).scalar_one_or_none()

    def find_experiment(self, ref: str) -> ExperimentRow | None:
        """Look up by full id, id prefix, or exact name (most recent)."""
        exp = self.get_experiment(ref)
        if exp is not None:
            return exp
        with self.db.session() as s:
            row = (
                s.execute(
                    select(ExperimentRow)
                    .where((ExperimentRow.id.like(f"{ref}%")) | (ExperimentRow.name == ref))
                    .order_by(ExperimentRow.created_at.desc())
                )
                .scalars()
                .first()
            )
            return self.get_experiment(row.id) if row else None

    def get_run(self, run_id: str) -> RunRow | None:
        with self.db.session() as s:
            return s.execute(
                select(RunRow)
                .where(RunRow.id == run_id)
                .options(
                    selectinload(RunRow.events),
                    selectinload(RunRow.verifier_result),
                    selectinload(RunRow.artifacts),
                    selectinload(RunRow.task),
                    selectinload(RunRow.variant),
                    selectinload(RunRow.experiment),
                )
            ).scalar_one_or_none()

    def find_run(self, ref: str) -> RunRow | None:
        run = self.get_run(ref)
        if run is not None:
            return run
        with self.db.session() as s:
            row = s.execute(select(RunRow).where(RunRow.id.like(f"{ref}%"))).scalars().first()
            return self.get_run(row.id) if row else None

    def get_events(self, run_id: str) -> list[EventRow]:
        with self.db.session() as s:
            return list(
                s.execute(
                    select(EventRow).where(EventRow.run_id == run_id).order_by(EventRow.sequence)
                )
                .scalars()
                .all()
            )

    def count_experiments(self) -> int:
        with self.db.session() as s:
            return int(s.execute(select(func.count(ExperimentRow.id))).scalar_one())

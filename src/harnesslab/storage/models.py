"""SQLAlchemy ORM models.

Large blobs (diffs, raw streams, verifier logs) are stored as files and
referenced from ``artifacts``; the database keeps queryable metadata and
capped text copies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ExperimentRow(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    suite_name: Mapped[str] = mapped_column(String(255))
    suite_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="running")
    repetitions: Mapped[int] = mapped_column(Integer, default=1)
    parallelism: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    harnesslab_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    harnesslab_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    environment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    variants: Mapped[list[VariantRow]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", order_by="VariantRow.position"
    )
    tasks: Mapped[list[TaskRow]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", order_by="TaskRow.position"
    )
    runs: Mapped[list[RunRow]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", order_by="RunRow.id"
    )


class VariantRow(Base):
    """HARNESS x MODEL x CONFIGURATION preset used in an experiment."""

    __tablename__ = "variants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    variant_key: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    runner: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_requested: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    harness_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_policy_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_policy_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    factors_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    experiment: Mapped[ExperimentRow] = relationship(back_populates="variants")
    runs: Mapped[list[RunRow]] = relationship(back_populates="variant")


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    task_key: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    task_hash: Mapped[str] = mapped_column(String(64))
    spec_hash: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    base_commit: Mapped[str] = mapped_column(String(64))
    repo_path: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    experiment: Mapped[ExperimentRow] = relationship(back_populates="tasks")
    runs: Mapped[list[RunRow]] = relationship(back_populates="task")


class RunRow(Base):
    """One TASK x VARIANT x REPETITION x ENVIRONMENT cell."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[str] = mapped_column(
        ForeignKey("variants.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    repetition: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    outcome: Mapped[str] = mapped_column(String(32), default="not_verified")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    worktree_kept: Mapped[bool] = mapped_column(Boolean, default=False)

    # Reproducibility record
    base_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    harnesslab_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    harnesslab_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runner: Mapped[str] = mapped_column(String(64))
    runner_config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    environment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    metrics_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    model_requested: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_resolved: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cli_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    runner_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Metrics (denormalized cache of metrics_json for queries and the dashboard)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verified_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shell_commands: Mapped[int | None] = mapped_column(Integer, nullable=True)
    files_changed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_added: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lines_deleted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verifier_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    events_count: Mapped[int] = mapped_column(Integer, default=0)

    experiment: Mapped[ExperimentRow] = relationship(back_populates="runs")
    variant: Mapped[VariantRow] = relationship(back_populates="runs")
    task: Mapped[TaskRow] = relationship(back_populates="runs")
    events: Mapped[list[EventRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="EventRow.sequence"
    )
    verifier_result: Mapped[VerifierResultRow | None] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    artifacts: Mapped[list[ArtifactRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ArtifactRow.kind"
    )


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_run_sequence", "run_id", "sequence", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    run: Mapped[RunRow] = relationship(back_populates="events")


class VerifierResultRow(Base):
    __tablename__ = "verifier_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), default="not_verified")
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    normalized_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    protected_violations_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    injected_files_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    skipped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_version: Mapped[str] = mapped_column(String(16), default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    run: Mapped[RunRow] = relationship(back_populates="verifier_result")


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)  # relative to the Harness Lab home directory
    media_type: Mapped[str] = mapped_column(String(64), default="text/plain")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    run: Mapped[RunRow] = relationship(back_populates="artifacts")

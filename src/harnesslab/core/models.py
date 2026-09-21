"""Provider-neutral specifications and results.

The central experimental object of Harness Lab is::

    TASK x MODEL x HARNESS x CONFIGURATION x ENVIRONMENT
        -> TRACE -> INDEPENDENT VERIFIER -> METRICS

* :class:`TaskSpec` is the TASK (prompt + fixture repository + verifier).
* :class:`VariantSpec` names a HARNESS (``runner``), a MODEL and an arbitrary
  CONFIGURATION (extra keys are preserved verbatim, so future harness
  components such as context or tool policies never require schema changes).
* :class:`EnvironmentSpec` records the ENVIRONMENT (sandbox kind, platform).
* One run = one (task, variant, repetition, environment) cell; its TRACE is a
  list of :class:`harnesslab.core.events.Event`; its verdict is a
  :class:`VerifierResult`; its :class:`RunMetrics` are derived from all three.
"""

from __future__ import annotations

import platform
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harnesslab.core.ids import hash_value

# ---------------------------------------------------------------------------
# Task specification
# ---------------------------------------------------------------------------


class RepoSpec(BaseModel):
    path: str
    base_ref: str = "HEAD"


class SetupSpec(BaseModel):
    commands: list[str] = Field(default_factory=list)
    timeout_seconds: int = 120


class InjectSpec(BaseModel):
    """A file copied into the worktree *only* at verification time."""

    source: str
    dest: str


class VerificationSpec(BaseModel):
    command: str
    score_command: str | None = None
    timeout_seconds: int = 120
    inject: list[InjectSpec] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)


class LimitsSpec(BaseModel):
    agent_timeout_seconds: int = 600


class ReferenceSolutionSpec(BaseModel):
    """Optional known-good solution (used by the fake runner and ``suite check``)."""

    overlay: str | None = None
    partial_overlay: str | None = None
    description: str | None = None


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    version: int = 1
    description: str | None = None
    repo: RepoSpec
    prompt: str
    setup: SetupSpec = Field(default_factory=SetupSpec)
    verification: VerificationSpec
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    tags: list[str] = Field(default_factory=list)
    reference_solution: ReferenceSolutionSpec | None = None
    source_path: Path | None = Field(default=None, exclude=True)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or any(ch in value for ch in "/\\ \t\n"):
            raise ValueError("task id must be a non-empty slug without spaces or slashes")
        return value

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else (self.base_dir / path).resolve()

    @property
    def repo_path(self) -> Path:
        return self.resolve(self.repo.path)

    def spec_hash(self) -> str:
        """Hash of the task definition (independent of where the file lives)."""
        return hash_value(self.model_dump(mode="json"))

    def prompt_hash(self) -> str:
        return hash_value(self.prompt)


# ---------------------------------------------------------------------------
# Variant / suite / experiment specifications
# ---------------------------------------------------------------------------

_VARIANT_KNOWN_FIELDS = {
    "id",
    "runner",
    "model",
    "description",
    "harness_version",
    "model_provider",
    "context_policy",
    "tool_policy",
    "skill_version",
}


class VariantSpec(BaseModel):
    """A named HARNESS x MODEL x CONFIGURATION preset.

    Unknown keys are runner options and are preserved verbatim in
    :attr:`options`, so adapters can grow new knobs without schema changes.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    runner: str
    model: str | None = None
    description: str | None = None
    harness_version: str | None = None
    model_provider: str | None = None
    context_policy: dict[str, Any] | None = None
    tool_policy: dict[str, Any] | None = None
    skill_version: str | None = None

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})

    def runner_config(self) -> RunnerConfig:
        return RunnerConfig(
            runner=self.runner,
            model=self.model,
            variant_id=self.id,
            options=self.options,
            context_policy=self.context_policy,
            tool_policy=self.tool_policy,
        )


class SuiteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    tasks: list[str] = Field(default_factory=list)
    variants: list[VariantSpec] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    source_path: Path | None = Field(default=None, exclude=True)

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    suite: str
    description: str | None = None
    repetitions: int = 1
    parallelism: int = 1
    variants: list[VariantSpec] = Field(default_factory=list)
    tasks: list[str] | None = None
    keep_worktrees: bool = False
    source_path: Path | None = Field(default=None, exclude=True)

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()


# ---------------------------------------------------------------------------
# Runner interface types
# ---------------------------------------------------------------------------


class RunnerConfig(BaseModel):
    """Everything a runner needs to know about *how* to run (never *where*)."""

    runner: str
    model: str | None = None
    variant_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    context_policy: dict[str, Any] | None = None
    tool_policy: dict[str, Any] | None = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def config_hash(self) -> str:
        return hash_value(self.model_dump(mode="json", exclude={"variant_id"}))


class UsageTotals(BaseModel):
    """Normalized token accounting.

    ``input_tokens`` are *uncached* prompt tokens; cached prompt tokens are
    reported separately so harnesses with different cache semantics stay
    comparable (Codex reports cached tokens inside ``input_tokens``, Claude
    Code reports them separately; adapters normalize to this shape).
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )

    def add(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
        )


class RunStatus(StrEnum):
    """Infrastructure state of a run (independent of the task outcome)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"  # the agent process finished on its own
    TIMEOUT = "timeout"  # killed by Harness Lab after agent_timeout_seconds
    CRASHED = "crashed"  # the adapter or process failed unexpectedly
    UNAVAILABLE = "unavailable"  # the harness CLI is not installed / usable
    BLOCKED = "blocked"  # the harness was denied permissions it needed
    INTERRUPTED = "interrupted"  # Harness Lab itself was interrupted

    @property
    def is_terminal(self) -> bool:
        return self not in (RunStatus.PENDING, RunStatus.RUNNING)


class Outcome(StrEnum):
    """Verifier verdict."""

    PASS = "pass"
    FAIL = "fail"
    NOT_VERIFIED = "not_verified"


class RunnerResult(BaseModel):
    status: RunStatus = RunStatus.COMPLETED
    exit_code: int | None = None
    final_message: str | None = None
    usage: UsageTotals = Field(default_factory=UsageTotals)
    usage_by_model: dict[str, UsageTotals] = Field(default_factory=dict)
    reported_cost_usd: float | None = None
    provider_session_id: str | None = None
    model_resolved: str | None = None
    cli_version: str | None = None
    num_turns: int | None = None
    permission_denials: int = 0
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Availability(BaseModel):
    runner: str
    available: bool
    executable: str | None = None
    version: str | None = None
    detail: str = ""


class EnvironmentSpec(BaseModel):
    """Where a run executed (the ENVIRONMENT axis)."""

    sandbox: str = "local-worktree"
    os: str = ""
    os_release: str = ""
    machine: str = ""
    python_version: str = ""
    git_version: str | None = None
    hostname_hash: str | None = None

    @classmethod
    def detect(
        cls, sandbox: str = "local-worktree", git_version: str | None = None
    ) -> EnvironmentSpec:
        import socket

        return cls(
            sandbox=sandbox,
            os=platform.system(),
            os_release=platform.release(),
            machine=platform.machine(),
            python_version=sys.version.split()[0],
            git_version=git_version,
            hostname_hash=hash_value(socket.gethostname(), 12),
        )

    def environment_hash(self) -> str:
        return hash_value(self.model_dump(mode="json", exclude={"hostname_hash"}))


# ---------------------------------------------------------------------------
# Verification and diff results
# ---------------------------------------------------------------------------


class FileDiffStat(BaseModel):
    path: str
    added: int = 0
    deleted: int = 0
    binary: bool = False


class DiffSummary(BaseModel):
    base_commit: str
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    files: list[FileDiffStat] = Field(default_factory=list)
    status_text: str = ""
    stat_text: str = ""
    diff_text: str = ""
    diff_truncated: bool = False
    capture_failed: bool = False
    error: str | None = None


class VerifierResult(BaseModel):
    passed: bool | None = None
    outcome: Outcome = Outcome.NOT_VERIFIED
    command: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    stdout: str = ""
    stderr: str = ""
    score: float | None = None
    max_score: float | None = None
    normalized_score: float | None = None
    score_metrics: dict[str, Any] = Field(default_factory=dict)
    score_command: str | None = None
    score_exit_code: int | None = None
    score_error: str | None = None
    protected_violations: list[str] = Field(default_factory=list)
    injected_files: list[str] = Field(default_factory=list)
    overwritten_files: list[str] = Field(default_factory=list)
    skipped_reason: str | None = None
    verifier_version: str = "1"

    @property
    def verified_score(self) -> float | None:
        if self.normalized_score is not None:
            return self.normalized_score
        if self.passed is None:
            return None
        return 1.0 if self.passed else 0.0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

METRICS_VERSION = "1"


class RunMetrics(BaseModel):
    metrics_version: str = METRICS_VERSION
    verified_pass: bool | None = None
    verified_score: float | None = None
    wall_time_seconds: float | None = None
    agent_wall_time_seconds: float | None = None
    verifier_wall_time_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    reported_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    pricing_version: str | None = None
    tool_calls: int = 0
    tool_calls_unfinished: int = 0
    subagent_tool_calls: int = 0
    shell_commands: int = 0
    assistant_messages: int = 0
    reasoning_events: int = 0
    error_events: int = 0
    file_change_events: int = 0
    num_turns: int | None = None
    permission_denials: int = 0
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    agent_exit_code: int | None = None
    verifier_exit_code: int | None = None
    events_total: int = 0

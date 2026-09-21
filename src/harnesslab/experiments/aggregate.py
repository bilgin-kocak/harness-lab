"""Per-variant aggregates, task x variant matrix and pairwise comparison.

Aggregates never hide individual runs: every function works on
:class:`RunSample` records that the dashboard also lists in full.  No
composite "winner score" is computed; raw metrics are compared side by side.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.core.models import Outcome, RunStatus


class RunSample(BaseModel):
    run_id: str
    task_key: str
    variant_key: str
    repetition: int = 0
    status: str = RunStatus.COMPLETED.value
    outcome: str = Outcome.NOT_VERIFIED.value
    verified_pass: bool | None = None
    verified_score: float | None = None
    wall_time_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    tool_calls: int | None = None
    shell_commands: int | None = None
    files_changed: int | None = None
    reported_cost_usd: float | None = None
    estimated_cost_usd: float | None = None

    @property
    def is_valid(self) -> bool:
        """A run counts for success rate only when a verifier verdict exists."""
        return self.verified_pass is not None


class Stat(BaseModel):
    n: int = 0
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    min: float | None = None
    max: float | None = None


def describe(values: list[float | int | None]) -> Stat:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return Stat()
    return Stat(
        n=len(clean),
        mean=statistics.fmean(clean),
        median=statistics.median(clean),
        std=statistics.stdev(clean) if len(clean) >= 2 else None,
        min=min(clean),
        max=max(clean),
    )


class VariantAggregate(BaseModel):
    variant_key: str
    n_total: int = 0
    n_valid: int = 0
    n_passed: int = 0
    n_failed: int = 0
    n_not_verified: int = 0
    n_infra_failures: int = 0  # timeout / crashed / unavailable / blocked / interrupted
    success_rate: float | None = None
    score: Stat = Field(default_factory=Stat)
    wall_time_seconds: Stat = Field(default_factory=Stat)
    input_tokens: Stat = Field(default_factory=Stat)
    output_tokens: Stat = Field(default_factory=Stat)
    cached_input_tokens: Stat = Field(default_factory=Stat)
    tool_calls: Stat = Field(default_factory=Stat)
    shell_commands: Stat = Field(default_factory=Stat)
    files_changed: Stat = Field(default_factory=Stat)
    reported_cost_usd: Stat = Field(default_factory=Stat)
    estimated_cost_usd: Stat = Field(default_factory=Stat)
    per_task_pass_rate: dict[str, float | None] = Field(default_factory=dict)


INFRA_FAILURE_STATUSES = {
    RunStatus.TIMEOUT.value,
    RunStatus.CRASHED.value,
    RunStatus.UNAVAILABLE.value,
    RunStatus.BLOCKED.value,
    RunStatus.INTERRUPTED.value,
}


def aggregate_variant(variant_key: str, samples: list[RunSample]) -> VariantAggregate:
    valid = [s for s in samples if s.is_valid]
    passed = [s for s in valid if s.verified_pass]
    agg = VariantAggregate(
        variant_key=variant_key,
        n_total=len(samples),
        n_valid=len(valid),
        n_passed=len(passed),
        n_failed=len(valid) - len(passed),
        n_not_verified=len(samples) - len(valid),
        n_infra_failures=sum(1 for s in samples if s.status in INFRA_FAILURE_STATUSES),
        success_rate=(len(passed) / len(valid)) if valid else None,
        score=describe([s.verified_score for s in valid]),
        wall_time_seconds=describe([s.wall_time_seconds for s in samples]),
        input_tokens=describe([s.input_tokens for s in samples]),
        output_tokens=describe([s.output_tokens for s in samples]),
        cached_input_tokens=describe([s.cached_input_tokens for s in samples]),
        tool_calls=describe([s.tool_calls for s in samples]),
        shell_commands=describe([s.shell_commands for s in samples]),
        files_changed=describe([s.files_changed for s in samples]),
        reported_cost_usd=describe([s.reported_cost_usd for s in samples]),
        estimated_cost_usd=describe([s.estimated_cost_usd for s in samples]),
    )
    by_task: dict[str, list[RunSample]] = defaultdict(list)
    for s in valid:
        by_task[s.task_key].append(s)
    for task_key, runs in by_task.items():
        agg.per_task_pass_rate[task_key] = sum(1 for r in runs if r.verified_pass) / len(runs)
    return agg


def aggregate_variants(
    samples: list[RunSample], variant_order: list[str] | None = None
) -> dict[str, VariantAggregate]:
    grouped: dict[str, list[RunSample]] = defaultdict(list)
    for s in samples:
        grouped[s.variant_key].append(s)
    keys = variant_order or sorted(grouped)
    result: dict[str, VariantAggregate] = {}
    for key in keys:
        result[key] = aggregate_variant(key, grouped.get(key, []))
    for key in grouped:
        if key not in result:
            result[key] = aggregate_variant(key, grouped[key])
    return result


class MatrixCell(BaseModel):
    task_key: str
    variant_key: str
    runs: list[RunSample] = Field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def n_passed(self) -> int:
        return sum(1 for r in self.runs if r.verified_pass)

    @property
    def n_valid(self) -> int:
        return sum(1 for r in self.runs if r.is_valid)

    @property
    def pass_rate(self) -> float | None:
        return self.n_passed / self.n_valid if self.n_valid else None

    @property
    def mean_score(self) -> float | None:
        scores = [r.verified_score for r in self.runs if r.verified_score is not None]
        return statistics.fmean(scores) if scores else None

    @property
    def state(self) -> str:
        """pass | fail | mixed | error | empty"""
        if not self.runs:
            return "empty"
        if self.n_valid == 0:
            return "error"
        if self.n_passed == self.n_valid:
            return "pass"
        if self.n_passed == 0:
            return "fail"
        return "mixed"

    @property
    def primary_run(self) -> RunSample | None:
        return self.runs[0] if self.runs else None


def build_matrix(
    samples: list[RunSample], task_order: list[str], variant_order: list[str]
) -> dict[str, dict[str, MatrixCell]]:
    matrix: dict[str, dict[str, MatrixCell]] = {
        t: {v: MatrixCell(task_key=t, variant_key=v) for v in variant_order} for t in task_order
    }
    for s in sorted(samples, key=lambda x: (x.repetition, x.run_id)):
        matrix.setdefault(s.task_key, {}).setdefault(
            s.variant_key, MatrixCell(task_key=s.task_key, variant_key=s.variant_key)
        ).runs.append(s)
    return matrix


class TaskComparison(BaseModel):
    task_key: str
    a_pass_rate: float | None
    b_pass_rate: float | None
    a_score: float | None
    b_score: float | None
    category: str  # both_passed | both_failed | a_only | b_only | mixed | unverified


class MetricDelta(BaseModel):
    metric: str
    label: str
    a: float | None
    b: float | None
    delta: float | None
    lower_is_better: bool = False


class VariantComparison(BaseModel):
    a: str
    b: str
    a_aggregate: VariantAggregate
    b_aggregate: VariantAggregate
    deltas: list[MetricDelta]
    tasks: list[TaskComparison]
    summary: dict[str, int]


def _classify(a_rate: float | None, b_rate: float | None) -> str:
    if a_rate is None or b_rate is None:
        return "unverified"
    a_pass = a_rate == 1.0
    b_pass = b_rate == 1.0
    a_fail = a_rate == 0.0
    b_fail = b_rate == 0.0
    if a_pass and b_pass:
        return "both_passed"
    if a_fail and b_fail:
        return "both_failed"
    if a_pass and b_fail:
        return "a_only"
    if b_pass and a_fail:
        return "b_only"
    return "mixed"


def compare_variants(
    samples: list[RunSample], a: str, b: str, task_order: list[str]
) -> VariantComparison:
    aggs = aggregate_variants(samples, [a, b])
    agg_a, agg_b = aggs[a], aggs[b]
    matrix = build_matrix(samples, task_order, [a, b])
    tasks: list[TaskComparison] = []
    summary: dict[str, int] = defaultdict(int)
    for task_key in task_order:
        cell_a, cell_b = matrix[task_key][a], matrix[task_key][b]
        category = _classify(cell_a.pass_rate, cell_b.pass_rate)
        summary[category] += 1
        tasks.append(
            TaskComparison(
                task_key=task_key,
                a_pass_rate=cell_a.pass_rate,
                b_pass_rate=cell_b.pass_rate,
                a_score=cell_a.mean_score,
                b_score=cell_b.mean_score,
                category=category,
            )
        )

    def delta(
        metric: str, label: str, va: float | None, vb: float | None, lower_is_better: bool = False
    ) -> MetricDelta:
        d = (vb - va) if (va is not None and vb is not None) else None
        return MetricDelta(
            metric=metric, label=label, a=va, b=vb, delta=d, lower_is_better=lower_is_better
        )

    deltas = [
        delta("success_rate", "Pass rate", agg_a.success_rate, agg_b.success_rate),
        delta("score", "Mean verified score", agg_a.score.mean, agg_b.score.mean),
        delta(
            "input_tokens",
            "Median input tokens",
            agg_a.input_tokens.median,
            agg_b.input_tokens.median,
            True,
        ),
        delta(
            "output_tokens",
            "Median output tokens",
            agg_a.output_tokens.median,
            agg_b.output_tokens.median,
            True,
        ),
        delta(
            "wall_time_seconds",
            "Median wall time (s)",
            agg_a.wall_time_seconds.median,
            agg_b.wall_time_seconds.median,
            True,
        ),
        delta(
            "tool_calls",
            "Median tool calls",
            agg_a.tool_calls.median,
            agg_b.tool_calls.median,
            True,
        ),
        delta(
            "files_changed",
            "Median files changed",
            agg_a.files_changed.median,
            agg_b.files_changed.median,
        ),
        delta(
            "reported_cost_usd",
            "Median reported cost (USD)",
            agg_a.reported_cost_usd.median,
            agg_b.reported_cost_usd.median,
            True,
        ),
        delta(
            "estimated_cost_usd",
            "Median estimated cost (USD)",
            agg_a.estimated_cost_usd.median,
            agg_b.estimated_cost_usd.median,
            True,
        ),
    ]
    for key in ("both_passed", "both_failed", "a_only", "b_only", "mixed", "unverified"):
        summary.setdefault(key, 0)
    return VariantComparison(
        a=a,
        b=b,
        a_aggregate=agg_a,
        b_aggregate=agg_b,
        deltas=deltas,
        tasks=tasks,
        summary=dict(summary),
    )


def samples_from_rows(
    runs: list[Any], tasks_by_id: dict[str, Any], variants_by_id: dict[str, Any]
) -> list[RunSample]:
    """Build samples from ORM run rows (tasks/variants looked up by row id)."""
    samples: list[RunSample] = []
    for run in runs:
        task = tasks_by_id.get(run.task_id)
        variant = variants_by_id.get(run.variant_id)
        if task is None or variant is None:
            continue
        samples.append(
            RunSample(
                run_id=run.id,
                task_key=task.task_key,
                variant_key=variant.variant_key,
                repetition=run.repetition,
                status=run.status,
                outcome=run.outcome,
                verified_pass=run.verified_pass,
                verified_score=run.verified_score,
                wall_time_seconds=run.wall_time_seconds,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                cached_input_tokens=run.cached_input_tokens,
                tool_calls=run.tool_calls,
                shell_commands=run.shell_commands,
                files_changed=run.files_changed,
                reported_cost_usd=run.reported_cost_usd,
                estimated_cost_usd=run.estimated_cost_usd,
            )
        )
    return samples

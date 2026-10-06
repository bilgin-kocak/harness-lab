"""Paired statistics for decisions made on small task suites.

Every comparison in Harness Lab is *paired by task*: both sides ran the same tasks from the same
base commit, so the unit of evidence is the per-task difference, not the individual run.  This
module turns run samples into per-task differences and summarises them with

* a task-level cluster bootstrap of the mean difference (percentile interval, seeded), and
* an exact two-sided sign test on tasks where one side did better (McNemar-style; ties dropped).

A verdict is only given when the interval excludes zero *and* enough tasks were paired; below
``min_tasks`` the honest answer is "not enough tasks", whatever the point estimate says.  No
third-party statistics dependency is needed.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:  # aggregate imports this module; RunSample is only needed for annotations
    from harnesslab.experiments.aggregate import RunSample

DEFAULT_RESAMPLES = 2000
DEFAULT_LEVEL = 0.95
DEFAULT_MIN_TASKS = 5

Verdict = Literal["better", "worse", "no evidence", "not enough tasks"]


class Interval(BaseModel):
    """Mean of per-task differences (B − A) with a bootstrap interval."""

    estimate: float
    low: float
    high: float
    level: float = DEFAULT_LEVEL
    p_positive: float  # share of bootstrap means above zero
    n: int


class PairedTask(BaseModel):
    task_key: str
    a_rate: float
    b_rate: float
    a_cost: float | None = None
    b_cost: float | None = None
    a_llm_calls: float | None = None
    b_llm_calls: float | None = None
    a_improve_ratio: float | None = None
    b_improve_ratio: float | None = None


class PairedComparison(BaseModel):
    """B relative to A over the tasks both sides have verified runs for."""

    a: str
    b: str
    n_tasks: int
    wins: int  # tasks where B's pass rate is higher
    losses: int  # tasks where B's pass rate is lower
    ties: int
    sign_test_p: float | None
    pass_rate_diff: Interval | None
    cost_kind: str
    cost_diff: Interval | None
    llm_calls_diff: Interval | None
    improve_ratio_diff: Interval | None = None  # improvement tasks: B's ratio minus A's
    min_tasks: int
    verdict: Verdict
    tasks: list[PairedTask]

    @property
    def enough_tasks(self) -> bool:
        return self.n_tasks >= self.min_tasks


def _cost(sample: RunSample, kind: str) -> float | None:
    if kind == "reported_cost_usd":
        return sample.reported_cost_usd
    if kind == "estimated_cost_usd":
        return sample.estimated_cost_usd
    if sample.input_tokens is None and sample.output_tokens is None:
        return None
    return float(
        (sample.input_tokens or 0) + (sample.cached_input_tokens or 0) + (sample.output_tokens or 0)
    )


def cost_kind_for(samples: list[RunSample]) -> str:
    """Reported cost when any run reported one, else the pricing estimate, else total tokens."""
    if any(s.reported_cost_usd is not None for s in samples):
        return "reported_cost_usd"
    if any(s.estimated_cost_usd is not None for s in samples):
        return "estimated_cost_usd"
    return "total_tokens"


def _mean(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return statistics.fmean(clean) if clean else None


def paired_tasks(
    samples: list[RunSample],
    a: str,
    b: str,
    *,
    task_order: list[str] | None = None,
    cost_kind: str | None = None,
) -> list[PairedTask]:
    """Per-task pass rates (and mean cost, llm_calls) of A and B over their valid runs.

    Only tasks where *both* sides have at least one verified run are paired; repetitions are
    averaged within a task first, so a task with five repetitions does not count five times.
    """
    kind = cost_kind or cost_kind_for([s for s in samples if s.variant_key in (a, b)])
    by: dict[tuple[str, str], list[RunSample]] = defaultdict(list)
    for s in samples:
        if s.variant_key in (a, b) and s.is_valid:
            by[(s.variant_key, s.task_key)].append(s)
    keys = task_order or sorted({task for (_, task) in by})
    out: list[PairedTask] = []
    for task in keys:
        runs_a, runs_b = by.get((a, task), []), by.get((b, task), [])
        if not runs_a or not runs_b:
            continue
        out.append(
            PairedTask(
                task_key=task,
                a_rate=sum(1 for r in runs_a if r.verified_pass) / len(runs_a),
                b_rate=sum(1 for r in runs_b if r.verified_pass) / len(runs_b),
                a_cost=_mean([_cost(r, kind) for r in runs_a]),
                b_cost=_mean([_cost(r, kind) for r in runs_b]),
                a_llm_calls=_mean([r.llm_calls for r in runs_a]),
                b_llm_calls=_mean([r.llm_calls for r in runs_b]),
                a_improve_ratio=_mean([r.improve_ratio for r in runs_a]),
                b_improve_ratio=_mean([r.improve_ratio for r in runs_b]),
            )
        )
    return out


def bootstrap_mean_diff(
    diffs: list[float],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    level: float = DEFAULT_LEVEL,
) -> Interval | None:
    """Percentile bootstrap interval of the mean of ``diffs`` (one value per task)."""
    n = len(diffs)
    if n == 0:
        return None
    estimate = statistics.fmean(diffs)
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(diffs, k=n)) for _ in range(max(1, resamples)))
    alpha = (1.0 - level) / 2.0
    low = means[max(0, math.floor(alpha * len(means)))]
    high = means[min(len(means) - 1, math.ceil((1.0 - alpha) * len(means)) - 1)]
    return Interval(
        estimate=estimate,
        low=low,
        high=high,
        level=level,
        p_positive=sum(1 for m in means if m > 0) / len(means),
        n=n,
    )


def exact_sign_test(wins: int, losses: int) -> float | None:
    """Two-sided exact binomial test of wins vs losses under p = 0.5 (ties excluded)."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2.0 * tail)


def verdict_for(interval: Interval | None, n_tasks: int, min_tasks: int) -> Verdict:
    if interval is None or n_tasks < min_tasks:
        return "not enough tasks"
    if interval.low > 0:
        return "better"
    if interval.high < 0:
        return "worse"
    return "no evidence"


def paired_comparison(
    samples: list[RunSample],
    a: str,
    b: str,
    *,
    task_order: list[str] | None = None,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    level: float = DEFAULT_LEVEL,
    min_tasks: int = DEFAULT_MIN_TASKS,
) -> PairedComparison:
    """Compare B against A task by task; the verdict is about B's pass rate."""
    kind = cost_kind_for([s for s in samples if s.variant_key in (a, b)])
    tasks = paired_tasks(samples, a, b, task_order=task_order, cost_kind=kind)
    rate_diffs = [t.b_rate - t.a_rate for t in tasks]
    cost_diffs = [
        t.b_cost - t.a_cost for t in tasks if t.a_cost is not None and t.b_cost is not None
    ]
    call_diffs = [
        t.b_llm_calls - t.a_llm_calls
        for t in tasks
        if t.a_llm_calls is not None and t.b_llm_calls is not None
    ]
    improve_diffs = [
        t.b_improve_ratio - t.a_improve_ratio
        for t in tasks
        if t.a_improve_ratio is not None and t.b_improve_ratio is not None
    ]
    wins = sum(1 for d in rate_diffs if d > 0)
    losses = sum(1 for d in rate_diffs if d < 0)
    pass_interval = bootstrap_mean_diff(rate_diffs, resamples=resamples, seed=seed, level=level)
    return PairedComparison(
        a=a,
        b=b,
        n_tasks=len(tasks),
        wins=wins,
        losses=losses,
        ties=len(tasks) - wins - losses,
        sign_test_p=exact_sign_test(wins, losses),
        pass_rate_diff=pass_interval,
        cost_kind=kind,
        cost_diff=bootstrap_mean_diff(cost_diffs, resamples=resamples, seed=seed, level=level),
        llm_calls_diff=bootstrap_mean_diff(call_diffs, resamples=resamples, seed=seed, level=level),
        improve_ratio_diff=bootstrap_mean_diff(
            improve_diffs, resamples=resamples, seed=seed, level=level
        ),
        min_tasks=min_tasks,
        verdict=verdict_for(pass_interval, len(tasks), min_tasks),
        tasks=tasks,
    )

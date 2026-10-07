"""Would choosing a harness per task beat one harness for all tasks?

Answered from runs that already exist, over the tasks every variant has verified runs on:

* the **best single** variant is the one with the highest mean per-task pass rate;
* the **oracle** picks the best variant for each task after seeing the results; the in-sample
  gap is how much higher its mean pass rate is.

The oracle's per-task maximum over several variants is biased upward when repetitions are few:
with identical variants and coin-flip outcomes, one of them is "best" on most tasks by chance.
The **held-out gain** removes that bias by splitting each cell's runs by repetition parity,
choosing per task (and the best single variant) on one half and scoring both choices on the
other, then averaging over the two directions.  It is the number to trust.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable

from pydantic import BaseModel, Field

from harnesslab.experiments.aggregate import RunSample

HELD_OUT_NOTE = "needs at least 2 repetitions per variant"
HALVES_NOTE = "no task has valid runs from every variant in both halves (even and odd repetitions)"


class RoutingGap(BaseModel):
    """Per-task selection against the best fixed variant (rates are fractions in [0, 1])."""

    variant_keys: list[str]
    n_tasks: int = 0  # tasks with at least one valid run from every variant
    n_excluded: int = 0  # requested tasks left out because some variant has no valid run
    excluded_variants: list[str] = Field(default_factory=list)  # variants with no valid run
    best_single: str | None = None
    best_single_rate: float | None = None  # mean per-task pass rate of best_single
    oracle_rate: float | None = None  # mean over tasks of the best variant's pass rate
    gap: float | None = None  # oracle_rate - best_single_rate (in sample, biased upward)
    n_improvable: int = 0  # tasks where some variant beats best_single
    per_task_best: dict[str, list[str]] = Field(default_factory=dict)
    held_out_gain: float | None = None  # routed - single, chosen on one half, scored on the other
    n_held_out_tasks: int = 0  # tasks with valid runs in both halves for every variant
    note: str | None = None

    def summary(self) -> str:
        """One plain-text line, e.g. for ``sweep report`` and ``experiment show``."""
        if self.best_single is None or self.gap is None:
            return self.note or "no task has valid runs from every variant"
        line = (
            f"best single {self.best_single} {self.best_single_rate:.0%} · best per task "
            f"{self.oracle_rate:.0%} ({self.gap * 100:+.0f} points, "
            f"{self.n_improvable} of {self.n_tasks} tasks)"
        )
        if self.held_out_gain is not None:
            line += f" · held out {self.held_out_gain * 100:+.0f} points"
            if self.n_held_out_tasks < self.n_tasks:
                line += f" over {self.n_held_out_tasks} tasks"
        else:
            line += f" · held out: {self.note or HELD_OUT_NOTE}"
        if self.n_excluded:
            line += f" · {self.n_excluded} tasks left out (not run validly by every variant)"
        if self.excluded_variants:
            line += f" · {', '.join(self.excluded_variants)} left out (no valid runs)"
        return line


Rates = dict[str, dict[str, float]]  # task -> variant -> pass rate


def _rates(runs: Iterable[RunSample]) -> Rates:
    passed: dict[tuple[str, str], int] = defaultdict(int)
    total: dict[tuple[str, str], int] = defaultdict(int)
    for s in runs:
        total[(s.task_key, s.variant_key)] += 1
        passed[(s.task_key, s.variant_key)] += 1 if s.verified_pass else 0
    rates: Rates = defaultdict(dict)
    for (task, variant), n in total.items():
        rates[task][variant] = passed[(task, variant)] / n
    return rates


def _complete(rates: Rates, task_keys: list[str], variant_keys: list[str]) -> list[str]:
    return [t for t in task_keys if all(v in rates.get(t, {}) for v in variant_keys)]


def _best_single(rates: Rates, tasks: list[str], variant_keys: list[str]) -> tuple[str, float]:
    means = {v: statistics.fmean(rates[t][v] for t in tasks) for v in variant_keys}
    best = max(variant_keys, key=lambda v: means[v])  # max keeps the first of equal values
    return best, means[best]


def _route(rates: dict[str, float], single: str, variant_keys: list[str]) -> str:
    """The task's best variant, staying with the best single one unless another strictly beats it."""
    top = max(variant_keys, key=lambda v: rates[v])
    return single if rates[single] >= rates[top] else top


def _held_out_gain(
    valid: list[RunSample], tasks: list[str], variant_keys: list[str]
) -> tuple[float | None, int, str | None]:
    halves = [
        _rates(s for s in valid if s.repetition % 2 == 0),
        _rates(s for s in valid if s.repetition % 2 == 1),
    ]
    usable = [t for t in tasks if all(_complete(h, [t], variant_keys) for h in halves)]
    if not usable:
        repeated = any(s.repetition % 2 == 1 for s in valid)
        return None, 0, HALVES_NOTE if repeated else HELD_OUT_NOTE
    diffs: list[float] = []
    for h in (0, 1):
        choose, score = halves[h], halves[1 - h]
        single, _ = _best_single(choose, usable, variant_keys)
        routed = statistics.fmean(score[t][_route(choose[t], single, variant_keys)] for t in usable)
        fixed = statistics.fmean(score[t][single] for t in usable)
        diffs.append(routed - fixed)
    return statistics.fmean(diffs), len(usable), None


def routing_gap(
    samples: list[RunSample], task_keys: list[str], variant_keys: list[str]
) -> RoutingGap | None:
    """Compare per-task selection with the best single variant.

    None with fewer than two variants or two tasks: then there is nothing to choose between.

    Only valid runs count. A variant without any valid run (skipped, or its harness unavailable)
    is left out and named; of the rest, only tasks on which every variant has at least one valid
    run count. Ties go to the earlier variant in ``variant_keys``.
    """
    if len(variant_keys) < 2 or len(task_keys) < 2:
        return None
    wanted_tasks, wanted_variants = set(task_keys), set(variant_keys)
    valid = [
        s
        for s in samples
        if s.is_valid and s.task_key in wanted_tasks and s.variant_key in wanted_variants
    ]
    ran = {s.variant_key for s in valid}
    excluded = [v for v in variant_keys if v not in ran]
    variant_keys = [v for v in variant_keys if v in ran]
    rates = _rates(valid)
    tasks = _complete(rates, task_keys, variant_keys) if len(variant_keys) > 1 else []
    result = RoutingGap(
        variant_keys=variant_keys,
        excluded_variants=excluded,
        n_tasks=len(tasks),
        n_excluded=len(task_keys) - len(tasks),
    )
    if len(variant_keys) < 2:
        result.note = "fewer than two variants have valid runs"
        return result
    if not tasks:
        result.note = "no task has valid runs from every variant"
        return result
    single, single_rate = _best_single(rates, tasks, variant_keys)
    task_best = {t: max(rates[t].values()) for t in tasks}
    result.best_single = single
    result.best_single_rate = single_rate
    result.oracle_rate = statistics.fmean(task_best.values())
    result.gap = result.oracle_rate - single_rate
    result.n_improvable = sum(1 for t in tasks if task_best[t] > rates[t][single])
    result.per_task_best = {
        t: [v for v in variant_keys if rates[t][v] == task_best[t]] for t in tasks
    }
    result.held_out_gain, result.n_held_out_tasks, result.note = _held_out_gain(
        valid, tasks, variant_keys
    )
    return result

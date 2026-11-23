"""Paired statistics with no domain knowledge.

Every comparison is paired by task: both sides ran the same tasks, so the unit of evidence is the
per-task difference. The functions here take plain numbers keyed by task and return summaries:

* :func:`bootstrap_mean_diff`: a percentile bootstrap interval of the mean of per-task
  differences (seeded, so a report can be reproduced exactly);
* :func:`exact_sign_test`: a two-sided exact binomial test of wins against losses, ties dropped;
* :func:`exact_mcnemar`: the same test on the discordant pairs of a paired binary outcome;
* :func:`paired_numeric`: differences of a numeric metric (for example a gate count), with the
  direction that counts as better, intervals for the mean difference and the ratio, wins, losses,
  ties and the sign test;
* :func:`paired_rates`: per-task pass rates of two configurations, with the same summary plus the
  exact McNemar test on per-task majority outcomes.

``bootstrap_mean_diff``, ``exact_sign_test`` and ``Interval`` moved here from
``harnesslab.experiments.stats``, which re-exports them unchanged.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

DEFAULT_RESAMPLES = 2000
DEFAULT_LEVEL = 0.95
TIE_TOLERANCE = 1e-9


class Interval(BaseModel):
    """Mean of per-task differences (B − A) with a bootstrap interval."""

    estimate: float
    low: float
    high: float
    level: float = DEFAULT_LEVEL
    p_positive: float  # share of bootstrap means above zero
    n: int


def _snap(diff: float) -> float:
    """Float noise is a tie: means that are equal on paper can differ in their last bits."""
    return 0.0 if abs(diff) <= TIE_TOLERANCE else diff


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


def exact_mcnemar(only_b: int, only_a: int) -> float | None:
    """Exact McNemar test: two-sided binomial test on the discordant pairs.

    ``only_b`` counts tasks where only B succeeded, ``only_a`` where only A did. It is the exact
    sign test applied to the discordant pairs, named for what it tests.
    """
    return exact_sign_test(only_b, only_a)


class PairedNumeric(BaseModel):
    """B relative to A on one numeric metric, over the tasks where both have a value."""

    metric: str
    lower_is_better: bool
    n: int
    tasks: list[str]
    mean_a: float | None
    mean_b: float | None
    mean_diff: Interval | None  # B − A
    median_diff: float | None
    ratio: Interval | None  # geometric mean of (B + offset) / (A + offset), interval on the ratio
    ratio_offset: float
    wins: int  # tasks where B is better in the metric's direction
    losses: int
    ties: int
    sign_test_p: float | None


def _ratio_interval(
    a: Sequence[float], b: Sequence[float], offset: float, resamples: int, seed: int, level: float
) -> Interval | None:
    logs = []
    for x, y in zip(a, b, strict=True):
        if x + offset <= 0 or y + offset <= 0:
            return None
        logs.append(math.log(y + offset) - math.log(x + offset))
    iv = bootstrap_mean_diff(logs, resamples=resamples, seed=seed, level=level)
    if iv is None:
        return None
    return Interval(
        estimate=math.exp(iv.estimate),
        low=math.exp(iv.low),
        high=math.exp(iv.high),
        level=iv.level,
        p_positive=iv.p_positive,
        n=iv.n,
    )


def paired_numeric(
    metric: str,
    a: Mapping[str, float | None],
    b: Mapping[str, float | None],
    *,
    lower_is_better: bool,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    level: float = DEFAULT_LEVEL,
    ratio_offset: float = 1.0,
) -> PairedNumeric:
    """Compare B with A task by task on ``metric``; tasks missing a value on either side drop."""
    tasks = [t for t in a if t in b and a[t] is not None and b[t] is not None]
    av = [float(a[t]) for t in tasks]  # type: ignore[arg-type]
    bv = [float(b[t]) for t in tasks]  # type: ignore[arg-type]
    diffs = [_snap(y - x) for x, y in zip(av, bv, strict=True)]
    better = [(-d if lower_is_better else d) for d in diffs]
    wins = sum(1 for d in better if d > 0)
    losses = sum(1 for d in better if d < 0)
    return PairedNumeric(
        metric=metric,
        lower_is_better=lower_is_better,
        n=len(tasks),
        tasks=tasks,
        mean_a=statistics.fmean(av) if av else None,
        mean_b=statistics.fmean(bv) if bv else None,
        mean_diff=bootstrap_mean_diff(diffs, resamples=resamples, seed=seed, level=level),
        median_diff=statistics.median(diffs) if diffs else None,
        ratio=_ratio_interval(av, bv, ratio_offset, resamples, seed, level),
        ratio_offset=ratio_offset,
        wins=wins,
        losses=losses,
        ties=len(diffs) - wins - losses,
        sign_test_p=exact_sign_test(wins, losses),
    )


class PairedRates(BaseModel):
    """B relative to A on per-task pass rates (repetitions averaged within a task first)."""

    n: int
    tasks: list[str]
    rate_a: float | None  # mean over tasks of the per-task rate
    rate_b: float | None
    diff: Interval | None  # mean of per-task (rate_b − rate_a)
    wins: int  # tasks where B's rate is higher
    losses: int
    ties: int
    sign_test_p: float | None
    majority_only_b: int  # tasks B passes in a majority of repetitions and A does not
    majority_only_a: int
    mcnemar_majority_p: float | None


def paired_rates(
    a: Mapping[str, Sequence[bool]],
    b: Mapping[str, Sequence[bool]],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    level: float = DEFAULT_LEVEL,
) -> PairedRates:
    """Per-task pass rates of A and B over their repetitions, paired by task.

    A task enters when both sides have at least one outcome. "Majority" means strictly more than
    half of a side's repetitions passed.
    """
    tasks = [t for t in a if t in b and len(a[t]) > 0 and len(b[t]) > 0]
    ra = {t: sum(a[t]) / len(a[t]) for t in tasks}
    rb = {t: sum(b[t]) / len(b[t]) for t in tasks}
    diffs = [_snap(rb[t] - ra[t]) for t in tasks]
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d < 0)
    maj_a = {t: sum(a[t]) * 2 > len(a[t]) for t in tasks}
    maj_b = {t: sum(b[t]) * 2 > len(b[t]) for t in tasks}
    only_b = sum(1 for t in tasks if maj_b[t] and not maj_a[t])
    only_a = sum(1 for t in tasks if maj_a[t] and not maj_b[t])
    return PairedRates(
        n=len(tasks),
        tasks=tasks,
        rate_a=statistics.fmean(ra.values()) if tasks else None,
        rate_b=statistics.fmean(rb.values()) if tasks else None,
        diff=bootstrap_mean_diff(diffs, resamples=resamples, seed=seed, level=level),
        wins=wins,
        losses=losses,
        ties=len(diffs) - wins - losses,
        sign_test_p=exact_sign_test(wins, losses),
        majority_only_b=only_b,
        majority_only_a=only_a,
        mcnemar_majority_p=exact_mcnemar(only_b, only_a),
    )

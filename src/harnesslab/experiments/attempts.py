"""Several attempts per task: what more attempts are worth.

With repetitions, a variant makes several attempts at every task. Two numbers describe them:

* **pass@k**: the chance that at least one of k attempts passes, estimated without bias from all
  n attempts with c passes (Chen et al., 2021): ``1 - C(n-c, k) / C(n, k)``. It is a ceiling:
  something would have to recognise which attempt passed.
* **best-of-k**: the chance that the attempt *picked by the task's visible check* passes the
  hidden tests. Of k attempts drawn at random, the pick is one that passes the visible check
  (``verification.visible_command``, recorded per run as ``visible_pass``), or any attempt when
  none does, with ties broken at random. It measures "several attempts plus a cheap check that
  the agent could also run" against a single attempt, whose expected result is pass@1, the plain
  pass rate.

Both are averaged over tasks, each task counting once (so pass@1 is the mean per-task pass rate,
which differs from the pooled pass rate when tasks have different numbers of attempts). Every row
covers the same tasks: those with at least ``k_max`` verified attempts, where ``k_max`` is the
(lower) median number of attempts per task, so one short task does not hide the rest. Best-of-k minus
pass@1 per task gets the same paired bootstrap interval and verdict as comparisons between
variants: a selector that does not help leaves the interval around zero.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from math import comb

from pydantic import BaseModel, Field

from harnesslab.experiments.aggregate import RunSample
from harnesslab.experiments.stats import (
    DEFAULT_MIN_TASKS,
    DEFAULT_RESAMPLES,
    TIE_TOLERANCE,
    Interval,
    Verdict,
    bootstrap_mean_diff,
    verdict_for,
)


def pass_at_k(n: int, c: int, k: int) -> float:
    """The chance that at least one of k of the n attempts (c of which pass) passes."""
    if not 1 <= k <= n:
        raise ValueError(f"k must be between 1 and the number of attempts ({n}), not {k}")
    return 1.0 - comb(n - c, k) / comb(n, k)


def best_of_k(attempts: list[tuple[bool, bool]], k: int) -> float:
    """The chance that the attempt picked by the visible check passes the hidden tests.

    ``attempts`` are ``(visible_pass, hidden_pass)`` pairs. Among k attempts drawn at random, the
    pick is uniform over those that pass the visible check, or over all k when none does. By
    symmetry the pick passes with probability ``a / m`` when the draw holds a visible pass (``a``
    of the ``m`` visible passes also pass the hidden tests) and with the plain pass rate of the
    visible failures otherwise.
    """
    n = len(attempts)
    if not 1 <= k <= n:
        raise ValueError(f"k must be between 1 and the number of attempts ({n}), not {k}")
    visible = [hidden for seen, hidden in attempts if seen]
    rest = [hidden for seen, hidden in attempts if not seen]
    no_visible_pass = comb(len(rest), k) / comb(n, k)
    from_visible = sum(visible) / len(visible) if visible else 0.0
    from_rest = sum(rest) / len(rest) if rest else 0.0
    return (1.0 - no_visible_pass) * from_visible + no_visible_pass * from_rest


class AttemptsRow(BaseModel):
    k: int
    pass_at_k: float  # mean over tasks
    best_of_k: float | None = None  # mean over tasks whose attempts all have a visible result


class VariantAttempts(BaseModel):
    variant_key: str
    n_tasks: int = 0  # tasks counted: at least k_max verified attempts
    n_short_tasks: int = 0  # tasks left out with fewer than k_max verified attempts
    k_max: int = 0  # the lower median number of verified attempts per task
    n_best_tasks: int = 0  # counted tasks where every attempt has a visible check result
    rows: list[AttemptsRow] = Field(default_factory=list)  # k = 1 .. k_max
    selector_gain: Interval | None = None  # best-of-k_max minus pass@1, per task
    verdict: Verdict = "not enough tasks"  # about selector_gain
    cost_per_attempt: float | None = None  # mean reported (else estimated) cost of one attempt
    note: str | None = None

    def row(self, k: int) -> AttemptsRow | None:
        return next((r for r in self.rows if r.k == k), None)

    def summary(self) -> str:
        """One plain-text line, e.g. for ``harnesslab run`` and ``experiment show``."""
        if self.k_max < 2:
            return self.note or "needs at least 2 attempts per task"
        first, last = self.row(1), self.row(self.k_max)
        assert first is not None and last is not None
        k = self.k_max
        line = f"pass@1 {first.pass_at_k:.0%} · pass@{k} {last.pass_at_k:.0%}"
        if last.best_of_k is not None and self.selector_gain is not None:
            gain = self.selector_gain
            part = self.n_best_tasks < self.n_tasks
            line += (
                f" · best-of-{k} {last.best_of_k:.0%}"
                + (f" over {self.n_best_tasks} of {self.n_tasks} tasks" if part else "")
                + f" ({gain.estimate * 100:+.0f} points over one attempt"
                + (" on those tasks" if part else "")
                + f", {self.verdict})"
            )
        else:
            line += f" · best-of-{k}: {self.note or 'needs a visible check'}"
        if self.n_short_tasks:
            plural = "s" if self.n_short_tasks != 1 else ""
            line += f" · {self.n_short_tasks} task{plural} with fewer attempts left out"
        if self.cost_per_attempt is not None:
            line += f" · {k} attempts cost about ${k * self.cost_per_attempt:.2f} per task"
        return line


def attempts_report(
    samples: list[RunSample],
    variant_keys: list[str],
    task_keys: list[str],
    *,
    min_tasks: int = DEFAULT_MIN_TASKS,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
) -> list[VariantAttempts]:
    """pass@k and best-of-k for every variant, over its verified attempts."""
    wanted = set(task_keys)
    by: dict[tuple[str, str], list[RunSample]] = defaultdict(list)
    for s in samples:
        if s.is_valid and s.task_key in wanted:
            by[(s.variant_key, s.task_key)].append(s)
    return [
        _variant(variant, task_keys, by, min_tasks=min_tasks, resamples=resamples, seed=seed)
        for variant in variant_keys
    ]


def _variant(
    variant: str,
    task_keys: list[str],
    by: dict[tuple[str, str], list[RunSample]],
    *,
    min_tasks: int,
    resamples: int,
    seed: int,
) -> VariantAttempts:
    attempts = {t: by[(variant, t)] for t in task_keys if by.get((variant, t))}
    result = VariantAttempts(variant_key=variant)
    if not attempts:
        result.note = "no verified attempts"
        return result
    result.k_max = statistics.median_low(len(runs) for runs in attempts.values())
    short = [t for t, runs in attempts.items() if len(runs) < result.k_max]
    attempts = {t: runs for t, runs in attempts.items() if t not in short}
    result.n_tasks, result.n_short_tasks = len(attempts), len(short)
    seen = {
        t: [(bool(r.visible_pass), bool(r.verified_pass)) for r in runs]
        for t, runs in attempts.items()
        if all(r.visible_pass is not None for r in runs)
    }
    result.n_best_tasks = len(seen)
    for k in range(1, result.k_max + 1):
        result.rows.append(
            AttemptsRow(
                k=k,
                pass_at_k=statistics.fmean(
                    pass_at_k(len(runs), sum(1 for r in runs if r.verified_pass), k)
                    for runs in attempts.values()
                ),
                best_of_k=statistics.fmean(best_of_k(pairs, k) for pairs in seen.values())
                if seen
                else None,
            )
        )
    costs = [
        r.reported_cost_usd if r.reported_cost_usd is not None else r.estimated_cost_usd
        for runs in attempts.values()
        for r in runs
    ]
    known = [c for c in costs if c is not None]
    result.cost_per_attempt = statistics.fmean(known) if known else None
    if result.k_max < 2:
        result.note = "needs at least 2 attempts per task (--repetitions)"
        return result
    if not seen:
        result.note = "best-of-k needs verification.visible_command on the tasks"
        return result
    gains = [
        best_of_k(pairs, result.k_max) - sum(hidden for _, hidden in pairs) / len(pairs)
        for pairs in seen.values()
    ]
    # A check that cannot tell attempts apart leaves only float noise, which is no gain.
    gains = [g if abs(g) > TIE_TOLERANCE else 0.0 for g in gains]
    result.selector_gain = bootstrap_mean_diff(gains, resamples=resamples, seed=seed)
    result.verdict = verdict_for(result.selector_gain, len(gains), min_tasks)
    if len(seen) < len(attempts):
        result.note = (
            f"best-of-k over {len(seen)} of {len(attempts)} tasks (the others have no visible "
            "check result)"
        )
    return result

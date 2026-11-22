"""Rank configurations from dev records, by the study's declared objective.

The core does not know what a metric means: the objective names metrics and says which
direction wins. Only dev records are ranked; held-out records never reach this module.
"""

from __future__ import annotations

import functools
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.study.spec import ObjectiveSpec


class ConfigScore(BaseModel):
    config_hash: str
    label: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    n_records: int = 0
    tasks_covered: int = 0
    complete: bool = False  # a record on every task the objective considers
    feasible: bool = False
    reason: str = ""
    pass_rate: float | None = None  # mean over tasks of the per-task pass rate
    passes: int = 0
    mean_cost_usd: float | None = None
    cost_per_pass_usd: float | None = None
    ratios: dict[str, float] = Field(default_factory=dict)  # baseline_ratio: geometric means
    medians: dict[str, float] = Field(default_factory=dict)
    rank: int | None = None


class Ranking(BaseModel):
    kind: str
    task_ids: list[str]  # the tasks the objective considered
    dropped_tasks: dict[str, str] = Field(default_factory=dict)  # task -> why it was left out
    baseline_hash: str
    scores: list[ConfigScore]  # ranked feasible ones first (rank 1 = best), then the rest

    @property
    def best(self) -> ConfigScore | None:
        return self.scores[0] if self.scores and self.scores[0].feasible else None

    def get(self, config_hash: str) -> ConfigScore | None:
        return next((s for s in self.scores if s.config_hash == config_hash), None)


def _passed(record: dict[str, Any]) -> bool:
    verdict = record.get("verdict") or {}
    return verdict.get("status") == "pass"


def _cost(record: dict[str, Any]) -> float:
    return float(((record.get("attempt") or {}).get("usage") or {}).get("cost_usd") or 0.0)


def _group(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        by[r["config_hash"]].append(r)
    return by


def _base_score(config_hash: str, recs: list[dict[str, Any]]) -> ConfigScore:
    first = recs[0]
    return ConfigScore(
        config_hash=config_hash,
        label=first.get("config_label"),
        values=dict(first.get("config") or {}),
        n_records=len(recs),
        tasks_covered=len({r["task_id"] for r in recs}),
        passes=sum(1 for r in recs if _passed(r)),
        mean_cost_usd=statistics.fmean(_cost(r) for r in recs) if recs else None,
    )


def rank_configs(
    records: list[dict[str, Any]],
    objective: ObjectiveSpec,
    baseline_hash: str,
    task_ids: list[str],
) -> Ranking:
    if objective.kind == "pass_rate":
        return _rank_pass_rate(records, objective, baseline_hash, task_ids)
    return _rank_baseline_ratio(records, objective, baseline_hash, task_ids)


# ---------------------------------------------------------------------------
# pass_rate
# ---------------------------------------------------------------------------


def _rank_pass_rate(
    records: list[dict[str, Any]], obj: ObjectiveSpec, baseline_hash: str, task_ids: list[str]
) -> Ranking:
    wanted = set(task_ids)
    scores: list[ConfigScore] = []
    for h, recs in _group(r for r in records if r["task_id"] in wanted).items():
        s = _base_score(h, recs)
        per_task: dict[str, list[bool]] = defaultdict(list)
        for r in recs:
            per_task[r["task_id"]].append(_passed(r))
        s.complete = set(per_task) == wanted
        s.pass_rate = statistics.fmean(sum(v) / len(v) for v in per_task.values())
        total_cost = sum(_cost(r) for r in recs)
        s.cost_per_pass_usd = total_cost / s.passes if s.passes else None
        scores.append(s)

    base = next((s for s in scores if s.config_hash == baseline_hash), None)
    for s in scores:
        if not s.complete:
            s.reason = f"incomplete: {s.tasks_covered} of {len(wanted)} tasks"
        elif obj.cost_cap_multiple is not None:
            if base is None or not base.complete or base.mean_cost_usd is None:
                s.reason = "the baseline has not been run on every task: no cost cap to apply"
            elif (s.mean_cost_usd or 0.0) > obj.cost_cap_multiple * base.mean_cost_usd + 1e-12:
                s.reason = (
                    f"mean cost per attempt ${s.mean_cost_usd:.4f} is above "
                    f"{obj.cost_cap_multiple:g} x the baseline's ${base.mean_cost_usd:.4f}"
                )
            else:
                s.feasible = True
        else:
            s.feasible = True

    feasible = [s for s in scores if s.feasible]
    best = max((s.pass_rate or 0.0 for s in feasible), default=0.0)

    def cpp(s: ConfigScore) -> float:
        return s.cost_per_pass_usd if s.cost_per_pass_usd is not None else math.inf

    def key(s: ConfigScore) -> tuple:
        if (s.pass_rate or 0.0) >= best - obj.tie_tolerance - 1e-12:
            return (0, cpp(s), s.config_hash)
        return (1, -(s.pass_rate or 0.0), cpp(s), s.config_hash)

    ordered = sorted(feasible, key=key) + sorted(
        (s for s in scores if not s.feasible),
        key=lambda s: (-(s.pass_rate or 0.0), s.config_hash),
    )
    for i, s in enumerate(ordered):
        s.rank = i + 1 if s.feasible else None
    return Ranking(
        kind="pass_rate", task_ids=list(task_ids), baseline_hash=baseline_hash, scores=ordered
    )


# ---------------------------------------------------------------------------
# baseline_ratio
# ---------------------------------------------------------------------------


def _metric(records: list[dict[str, Any]], name: str) -> float | None:
    values = [(r.get("metrics") or {}).get(name) for r in records]
    clean = [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if len(clean) != len(values) or not clean:
        return None
    return statistics.fmean(clean)


def _rank_baseline_ratio(
    records: list[dict[str, Any]], obj: ObjectiveSpec, baseline_hash: str, task_ids: list[str]
) -> Ranking:
    by_cfg_task: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r["task_id"] in task_ids:
            by_cfg_task[r["config_hash"]][r["task_id"]].append(r)

    base_tasks = by_cfg_task.get(baseline_hash, {})
    considered: list[str] = []
    dropped: dict[str, str] = {}
    for t in task_ids:
        recs = base_tasks.get(t)
        if not recs:
            dropped[t] = "the baseline has no record on this task"
        elif obj.drop_tasks_where_baseline_unverified and not all(_passed(r) for r in recs):
            status = (recs[0].get("verdict") or {}).get("status")
            dropped[t] = f"the baseline output did not verify ({status})"
        elif any(_metric(recs, m) is None for m in obj.metrics):
            dropped[t] = "the baseline has no value for an objective metric"
        else:
            considered.append(t)

    scores: list[ConfigScore] = []
    for h, tasks in by_cfg_task.items():
        recs = [r for t in considered for r in tasks.get(t, [])]
        all_recs = [r for rs in tasks.values() for r in rs]
        s = _base_score(h, all_recs)
        covered = [t for t in considered if tasks.get(t)]
        s.complete = len(covered) == len(considered) and bool(considered)
        if not considered:
            s.reason = "no task is usable: the baseline did not verify on any dev task"
        elif not s.complete:
            s.reason = f"incomplete: {len(covered)} of {len(considered)} considered tasks"
        elif not all(_passed(r) for r in recs):
            bad = sorted({r["task_id"] for r in recs if not _passed(r)})
            s.reason = f"did not verify on {len(bad)} task(s): {', '.join(bad[:5])}"
        else:
            s.feasible = True
        if s.complete:
            for m in obj.metrics:
                logs = []
                for t in considered:
                    v, vb = _metric(tasks[t], m), _metric(base_tasks[t], m)
                    if v is None or vb is None or v + obj.offset <= 0 or vb + obj.offset <= 0:
                        logs = []
                        break
                    logs.append(math.log((v + obj.offset) / (vb + obj.offset)))
                if logs:
                    s.ratios[m] = math.exp(statistics.fmean(logs))
                elif s.feasible:
                    s.feasible = False
                    s.reason = f"no value for metric {m!r} on some task"
            for m in obj.then:
                vals = [_metric(tasks[t], m) for t in considered]
                if all(v is not None for v in vals):
                    s.medians[m] = statistics.median(vals)  # type: ignore[type-var]
        scores.append(s)

    tol = obj.tie_tolerance

    def compare(a: ConfigScore, b: ConfigScore) -> int:
        for m in obj.metrics:
            x, y = a.ratios.get(m, math.inf), b.ratios.get(m, math.inf)
            if abs(x - y) > tol and x != y:
                return -1 if x < y else 1
        for m in obj.then:
            x, y = a.medians.get(m, math.inf), b.medians.get(m, math.inf)
            if x != y:
                return -1 if x < y else 1
        return -1 if a.config_hash < b.config_hash else (1 if a.config_hash > b.config_hash else 0)

    feasible = sorted((s for s in scores if s.feasible), key=functools.cmp_to_key(compare))
    rest = sorted((s for s in scores if not s.feasible), key=lambda s: s.config_hash)
    for i, s in enumerate(feasible):
        s.rank = i + 1
    return Ranking(
        kind="baseline_ratio",
        task_ids=considered,
        dropped_tasks=dropped,
        baseline_hash=baseline_hash,
        scores=feasible + rest,
    )


def primary_score(score: ConfigScore, obj: ObjectiveSpec) -> float | None:
    """The objective's first criterion, oriented so that higher is better."""
    if obj.kind == "pass_rate":
        return score.pass_rate
    value = score.ratios.get(obj.metrics[0])
    return None if value is None else -value

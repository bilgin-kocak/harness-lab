"""Configuration sweeps: expand a factor grid into variants, enforce a budget,
and pick the cheapest *verified* configuration per workload.

Objective values per run follow ``objective.minimize``:

* ``cost``   -> ``reported_cost_usd`` when any run reported one, else ``estimated_cost_usd``
                (from ``pricing.yaml``), else total tokens (the report names the kind used);
* ``tokens`` -> input + cached + output tokens;
* ``wall_time`` -> wall-clock seconds.

A configuration is *eligible* for a workload when its verified pass rate over
valid runs meets ``require.min_pass_rate`` **and** it has at least
``require.min_valid_runs`` valid runs (default: every planned run).  The
recommendation is the eligible configuration with the lowest median objective
(ties broken by ``tie_breaker``).  Marginal factor effects and a Pareto
frontier are reported alongside so the choice can be inspected, never just
trusted.
"""

from __future__ import annotations

import copy
import itertools
import random
import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.core.models import RunStatus, SweepSpec, VariantSpec
from harnesslab.experiments.aggregate import RunSample, samples_from_rows

# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


def _levels(factor: str, spec_levels: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(spec_levels, dict):
        return [(str(name), dict(overrides or {})) for name, overrides in spec_levels.items()]
    return [(str(value), {factor: value}) for value in spec_levels]


def config_id(factors: dict[str, str]) -> str:
    return "|".join(f"{k}={v}" for k, v in factors.items())


def expand_sweep(spec: SweepSpec) -> list[VariantSpec]:
    """Cartesian product of factor levels merged over ``base_variant`` (then optional sampling)."""
    names = list(spec.factors)
    level_lists = [_levels(name, spec.factors[name]) for name in names]
    variants: list[VariantSpec] = []
    for combo in itertools.product(*level_lists):
        merged = copy.deepcopy(spec.base_variant)
        factors: dict[str, str] = {}
        for name, (level_name, overrides) in zip(names, combo, strict=True):
            factors[name] = level_name
            for key, value in overrides.items():
                merged[key] = copy.deepcopy(value)
        merged.pop("id", None)
        merged.pop("factors", None)
        variants.append(VariantSpec(id=config_id(factors), factors=factors, **merged))
    if spec.sample and spec.sample.max_configs < len(variants):
        rng = random.Random(spec.sample.seed)
        chosen = set(rng.sample(range(len(variants)), spec.sample.max_configs))
        variants = [v for i, v in enumerate(variants) if i in chosen]
    return variants


# ---------------------------------------------------------------------------
# Budget gate
# ---------------------------------------------------------------------------


class BudgetGate:
    """Stops launching new runs once ``max_runs`` or ``max_cost_usd`` is exceeded."""

    def __init__(self, max_runs: int | None = None, max_cost_usd: float | None = None) -> None:
        self.max_runs = max_runs
        self.max_cost_usd = max_cost_usd
        self.started = 0
        self.spent_usd = 0.0
        self.skipped = 0

    def check(self) -> str | None:
        if self.max_runs is not None and self.started >= self.max_runs:
            self.skipped += 1
            return f"budget: max_runs {self.max_runs} reached"
        if self.max_cost_usd is not None and self.spent_usd >= self.max_cost_usd:
            self.skipped += 1
            return f"budget: max_cost_usd {self.max_cost_usd} exceeded (spent {self.spent_usd:.4f})"
        self.started += 1
        return None

    def on_finished(
        self, reported_cost_usd: float | None, estimated_cost_usd: float | None
    ) -> None:
        cost = reported_cost_usd if reported_cost_usd is not None else estimated_cost_usd
        if cost:
            self.spent_usd += float(cost)


GateFn = Callable[[], str | None]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class ConfigResult(BaseModel):
    variant_key: str
    factors: dict[str, str] = Field(default_factory=dict)
    n_total: int = 0
    n_valid: int = 0
    n_passed: int = 0
    n_skipped: int = 0
    pass_rate: float | None = None
    objective: float | None = None  # median over valid runs
    objective_kind: str = "cost"
    median_wall_time: float | None = None
    median_tokens: float | None = None
    median_tool_calls: float | None = None
    median_files_changed: float | None = None
    eligible: bool = False
    reason: str | None = None


class HoldoutResult(BaseModel):
    task_keys: list[str]
    n_valid: int = 0
    pass_rate: float | None = None
    objective: float | None = None


class WorkloadReport(BaseModel):
    workload: str
    kind: str  # suite | task | tag
    task_keys: list[str]
    configs: list[ConfigResult]
    recommended: ConfigResult | None = None
    runner_up: ConfigResult | None = None
    best_effort: ConfigResult | None = None  # highest pass rate when nothing is eligible
    pareto: list[str] = Field(default_factory=list)
    holdout: HoldoutResult | None = None


class FactorEffect(BaseModel):
    factor: str
    level: str
    n_configs: int
    mean_pass_rate: float | None
    median_objective: float | None


class SweepReport(BaseModel):
    name: str
    objective_kind: str
    minimize: str
    min_pass_rate: float
    min_valid_runs: int | None
    workloads: list[WorkloadReport]
    factor_effects: list[FactorEffect]
    n_configs: int
    n_runs: int
    n_skipped: int
    notes: list[str] = Field(default_factory=list)


def _objective_kind(spec: SweepSpec, samples: list[RunSample]) -> str:
    if spec.objective.minimize == "tokens":
        return "total_tokens"
    if spec.objective.minimize == "wall_time":
        return "wall_time_seconds"
    if any(s.reported_cost_usd is not None for s in samples):
        return "reported_cost_usd"
    if any(s.estimated_cost_usd is not None for s in samples):
        return "estimated_cost_usd"
    return "total_tokens"


def _tokens(s: RunSample) -> float | None:
    if s.input_tokens is None and s.output_tokens is None:
        return None
    return float((s.input_tokens or 0) + (s.cached_input_tokens or 0) + (s.output_tokens or 0))


def _value(kind: str, s: RunSample) -> float | None:
    if kind == "reported_cost_usd":
        return s.reported_cost_usd
    if kind == "estimated_cost_usd":
        return s.estimated_cost_usd
    if kind == "wall_time_seconds":
        return s.wall_time_seconds
    return _tokens(s)


def _median(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return statistics.median(clean) if clean else None


def _tie_value(spec: SweepSpec, c: ConfigResult) -> float:
    key = spec.objective.tie_breaker
    value = {
        "wall_time_seconds": c.median_wall_time,
        "tokens": c.median_tokens,
        "cost": c.objective,
    }[key]
    return float("inf") if value is None else float(value)


def _config_result(
    variant_key: str,
    factors: dict[str, str],
    runs: list[RunSample],
    kind: str,
    min_pass_rate: float,
    min_valid_runs: int,
) -> ConfigResult:
    valid = [r for r in runs if r.verified_pass is not None]
    passed = [r for r in valid if r.verified_pass]
    result = ConfigResult(
        variant_key=variant_key,
        factors=factors,
        n_total=len(runs),
        n_valid=len(valid),
        n_passed=len(passed),
        n_skipped=sum(1 for r in runs if r.status == RunStatus.SKIPPED.value),
        pass_rate=(len(passed) / len(valid)) if valid else None,
        objective=_median([_value(kind, r) for r in valid]),
        objective_kind=kind,
        median_wall_time=_median([r.wall_time_seconds for r in valid]),
        median_tokens=_median([_tokens(r) for r in valid]),
        median_tool_calls=_median([r.tool_calls for r in valid]),
        median_files_changed=_median([r.files_changed for r in valid]),
    )
    if not valid:
        result.reason = "no verified runs"
    elif result.n_valid < min_valid_runs:
        result.reason = f"only {result.n_valid} of {min_valid_runs} required valid runs"
    elif result.pass_rate is None or result.pass_rate < min_pass_rate:
        result.reason = f"pass rate {result.pass_rate:.0%} below {min_pass_rate:.0%}"
    elif result.objective is None:
        result.reason = f"no {kind} data"
    else:
        result.eligible = True
    return result


def _pareto(configs: list[ConfigResult]) -> list[str]:
    front: list[str] = []
    candidates = [c for c in configs if c.pass_rate is not None and c.objective is not None]
    for c in candidates:
        dominated = any(
            (o.pass_rate >= c.pass_rate and o.objective <= c.objective)
            and (o.pass_rate > c.pass_rate or o.objective < c.objective)
            for o in candidates
            if o is not c
        )
        if not dominated:
            front.append(c.variant_key)
    return front


def analyze_sweep(
    spec: SweepSpec,
    samples: list[RunSample],
    variant_factors: dict[str, dict[str, str]],
    task_tags: dict[str, list[str]],
) -> SweepReport:
    """Compute recommendations, Pareto fronts and factor effects from run samples.

    ``variant_factors`` maps variant key -> factor levels; ``task_tags`` maps
    task key -> tags (its keys define the task order).
    """
    kind = _objective_kind(spec, samples)
    all_tasks = list(task_tags)
    holdout = [t for t in spec.holdout_tasks if t in task_tags]
    search_tasks = [t for t in all_tasks if t not in holdout]
    notes: list[str] = []
    if spec.objective.minimize == "cost" and kind == "total_tokens":
        notes.append(
            "No cost was reported or estimated; configurations are ranked by total tokens instead."
        )
    if holdout:
        notes.append(
            f"Holdout tasks ({', '.join(holdout)}) were excluded from selection and are reported separately."
        )

    by_variant: dict[str, list[RunSample]] = defaultdict(list)
    for s in samples:
        by_variant[s.variant_key].append(s)
    variant_keys = [v for v in variant_factors if v in by_variant] + [
        v for v in by_variant if v not in variant_factors
    ]

    if spec.workload_by == "task":
        workload_defs = [(t, "task", [t]) for t in search_tasks]
    elif spec.workload_by == "tag":
        tags: dict[str, list[str]] = defaultdict(list)
        for t in search_tasks:
            for tag in task_tags.get(t, []):
                tags[tag].append(t)
        workload_defs = [(tag, "tag", tasks) for tag, tasks in sorted(tags.items())]
    else:
        workload_defs = [("suite", "suite", search_tasks)]

    workloads: list[WorkloadReport] = []
    for name, wkind, tasks in workload_defs:
        min_valid = spec.objective.require.min_valid_runs
        if min_valid is None:
            min_valid = spec.repetitions * len(tasks)
        configs: list[ConfigResult] = []
        for vk in variant_keys:
            runs = [r for r in by_variant[vk] if r.task_key in tasks]
            configs.append(
                _config_result(
                    vk,
                    variant_factors.get(vk, {}),
                    runs,
                    kind,
                    spec.objective.require.min_pass_rate,
                    min_valid,
                )
            )
        eligible = sorted(
            (c for c in configs if c.eligible), key=lambda c: (c.objective, _tie_value(spec, c))
        )
        ordered = eligible + sorted(
            (c for c in configs if not c.eligible),
            key=lambda c: (
                -(c.pass_rate if c.pass_rate is not None else -1.0),
                c.objective if c.objective is not None else float("inf"),
            ),
        )
        report = WorkloadReport(
            workload=name,
            kind=wkind,
            task_keys=tasks,
            configs=ordered,
            recommended=eligible[0] if eligible else None,
            runner_up=eligible[1] if len(eligible) > 1 else None,
            best_effort=None
            if eligible
            else next((c for c in ordered if c.pass_rate is not None), None),
            pareto=_pareto(configs),
        )
        if holdout and report.recommended is not None:
            runs = [r for r in by_variant[report.recommended.variant_key] if r.task_key in holdout]
            valid = [r for r in runs if r.verified_pass is not None]
            report.holdout = HoldoutResult(
                task_keys=holdout,
                n_valid=len(valid),
                pass_rate=(sum(1 for r in valid if r.verified_pass) / len(valid))
                if valid
                else None,
                objective=_median([_value(kind, r) for r in valid]),
            )
        workloads.append(report)

    # Marginal factor effects over the whole search set (all search tasks pooled).
    pooled: dict[str, ConfigResult] = {}
    for vk in variant_keys:
        runs = [r for r in by_variant[vk] if r.task_key in search_tasks]
        pooled[vk] = _config_result(
            vk, variant_factors.get(vk, {}), runs, kind, spec.objective.require.min_pass_rate, 1
        )
    effects: list[FactorEffect] = []
    for factor in spec.factors:
        level_groups: dict[str, list[ConfigResult]] = defaultdict(list)
        for vk, factors in variant_factors.items():
            if vk in pooled and factor in factors:
                level_groups[factors[factor]].append(pooled[vk])
        for level, group in level_groups.items():
            rates = [c.pass_rate for c in group if c.pass_rate is not None]
            effects.append(
                FactorEffect(
                    factor=factor,
                    level=level,
                    n_configs=len(group),
                    mean_pass_rate=(sum(rates) / len(rates)) if rates else None,
                    median_objective=_median([c.objective for c in group]),
                )
            )

    return SweepReport(
        name=spec.name,
        objective_kind=kind,
        minimize=spec.objective.minimize,
        min_pass_rate=spec.objective.require.min_pass_rate,
        min_valid_runs=spec.objective.require.min_valid_runs,
        workloads=workloads,
        factor_effects=effects,
        n_configs=len(variant_keys),
        n_runs=len(samples),
        n_skipped=sum(1 for s in samples if s.status == RunStatus.SKIPPED.value),
        notes=notes,
    )


def sweep_spec_from_experiment(exp_row: Any) -> SweepSpec | None:
    data = (exp_row.spec_json or {}).get("sweep")
    if not data:
        return None
    return SweepSpec(**data)


def report_for_experiment(exp_row: Any) -> SweepReport | None:
    """Recompute the sweep report of a stored experiment (None if it was not a sweep)."""
    spec = sweep_spec_from_experiment(exp_row)
    if spec is None:
        return None
    tasks_by_id = {t.id: t for t in exp_row.tasks}
    variants_by_id = {v.id: v for v in exp_row.variants}
    samples = samples_from_rows(exp_row.runs, tasks_by_id, variants_by_id)
    variant_factors = {
        v.variant_key: dict(v.factors_json) for v in exp_row.variants if v.factors_json
    }
    task_tags = {t.task_key: list(t.tags_json or []) for t in exp_row.tasks}
    return analyze_sweep(spec, samples, variant_factors, task_tags)

"""Compute a study's results from its records: no narrative, only numbers with their provenance.

Only valid records reach this module (no harness errors, one per cell). The held-out section
states whether every planned cell has a record; a task where either side of a comparison is
missing a planned record is listed as ``not_run`` and left out of that comparison, never counted
as a loss.

Held-out comparisons follow the objective's kind:

* ``pass_rate`` studies compare per-task pass rates (paired bootstrap, sign test, exact McNemar on
  majority outcomes), pass@1 and pass@k, mixed outcomes across repetitions, refusals, and cost
  and time per task;
* ``baseline_ratio`` studies compare each metric on the tasks where both sides verified. A task
  where the baseline verified and the other side did not counts as a loss in the sign test
  (``sign_test_p_with_failures``, the pre-registered test) and is listed; a task where the
  baseline did not verify is left out and listed. Ratios of the objective's metrics use its
  offset; ratios of other metrics (cost, time) use none.

Comparisons are repeated for each task tag listed in ``stats.tag_groups`` (for example a pool).
Robustness configurations (one knob replaced by several levels) are compared on per-task means
over those levels.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from harnesslab.study.objective import Ranking
from harnesslab.study.spec import Split, StudySpec
from harnesslab.study.stats import exact_sign_test, paired_numeric, paired_rates

ByTask = dict[str, list[dict[str, Any]]]


def _passed(r: dict[str, Any]) -> bool:
    return (r.get("verdict") or {}).get("status") == "pass"


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _usage(r: dict[str, Any], key: str) -> float:
    return float(((r.get("attempt") or {}).get("usage") or {}).get(key) or 0.0)


def _charged(r: dict[str, Any]) -> float:
    if r.get("charged_usd") is not None:
        return float(r["charged_usd"])
    return _usage(r, "cost_usd")


def _summary(recs: list[dict[str, Any]], metric_names: list[str]) -> dict[str, Any]:
    walls = [_num((r.get("attempt") or {}).get("wall_time_s")) for r in recs]
    walls = [w for w in walls if w is not None]
    passes = sum(1 for r in recs if _passed(r))
    charged = sum(_charged(r) for r in recs)
    out: dict[str, Any] = {
        "records": len(recs),
        "tasks": len({r["task_id"] for r in recs}),
        "verdicts": dict(Counter((r.get("verdict") or {}).get("status", "none") for r in recs)),
        "attempt_status": dict(Counter((r.get("attempt") or {}).get("status") for r in recs)),
        "passes": passes,
        "cost_usd": sum(_usage(r, "cost_usd") for r in recs),
        "charged_usd": charged,
        "cost_per_pass_usd": charged / passes if passes else None,
        "llm_calls": int(sum(_usage(r, "llm_calls") for r in recs)),
        "input_tokens": int(sum(_usage(r, "input_tokens") for r in recs)),
        "output_tokens": int(sum(_usage(r, "output_tokens") for r in recs)),
        "cache_read_tokens": int(sum(_usage(r, "cache_read_tokens") for r in recs)),
        "cache_write_tokens": int(sum(_usage(r, "cache_write_tokens") for r in recs)),
        "wall_time_s": {
            "mean": statistics.fmean(walls) if walls else None,
            "median": statistics.median(walls) if walls else None,
            "total": sum(walls),
        },
        "metrics": {},
    }
    for m in metric_names:
        vals = [_num((r.get("metrics") or {}).get(m)) for r in recs if _passed(r)]
        vals = [v for v in vals if v is not None]
        out["metrics"][m] = {
            "n_verified": len(vals),
            "mean": statistics.fmean(vals) if vals else None,
            "median": statistics.median(vals) if vals else None,
            "total": sum(vals) if vals else None,
        }
    return out


def _by_config_task(recs: list[dict[str, Any]]) -> dict[str, ByTask]:
    out: dict[str, ByTask] = defaultdict(lambda: defaultdict(list))
    for r in recs:
        out[r["config_hash"]][r["task_id"]].append(r)
    return out


def _split_run(
    tasks: Iterable[str], a: ByTask, b: ByTask, need_a: int, need_b: int
) -> tuple[list[str], list[str]]:
    """Tasks where both sides have every planned record, and the others."""
    run, not_run = [], []
    for t in sorted(tasks):
        if len(a.get(t, [])) >= need_a and len(b.get(t, [])) >= need_b:
            run.append(t)
        else:
            not_run.append(t)
    return run, not_run


def _metric_comparison(
    name: str,
    a: ByTask,
    b: ByTask,
    metric: str,
    *,
    tasks: Iterable[str],
    need_a: int,
    need_b: int,
    lower_is_better: bool,
    ratio_offset: float,
    stats: dict[str, Any],
) -> dict[str, Any]:
    run, not_run = _split_run(tasks, a, b, need_a, need_b)
    a_ok = {t: all(_passed(r) for r in a[t]) for t in run}
    b_ok = {t: all(_passed(r) for r in b[t]) for t in run}
    excluded = [t for t in run if not a_ok[t]]
    b_failed = [t for t in run if a_ok[t] and not b_ok[t]]
    both = [t for t in run if a_ok[t] and b_ok[t]]

    def mean_metric(recs: list[dict[str, Any]]) -> float | None:
        vals = [_num((r.get("metrics") or {}).get(metric)) for r in recs]
        return None if any(v is None for v in vals) or not vals else statistics.fmean(vals)  # type: ignore[arg-type]

    av = {t: mean_metric(a[t]) for t in both}
    bv = {t: mean_metric(b[t]) for t in both}
    cmp = paired_numeric(
        metric, av, bv, lower_is_better=lower_is_better, ratio_offset=ratio_offset, **stats
    )
    data = cmp.model_dump(mode="json")
    data.update(
        name=name,
        not_run=not_run,
        excluded_baseline_unverified=excluded,
        losses_unverified=b_failed,
        sign_test_p_with_failures=exact_sign_test(cmp.wins, cmp.losses + len(b_failed)),
        per_task=[{"task": t, "a": av[t], "b": bv[t]} for t in both],
    )
    return data


def _side(recs_by_task: ByTask, tasks: list[str], reps: int) -> dict[str, Any]:
    recs = [r for t in tasks for r in recs_by_task[t]]
    passes = sum(1 for r in recs if _passed(r))
    charged = sum(_charged(r) for r in recs)
    per_task = {t: sum(1 for r in recs_by_task[t] if _passed(r)) for t in tasks}
    return {
        "pass_at_1": statistics.fmean(per_task[t] / len(recs_by_task[t]) for t in tasks)
        if tasks
        else None,
        f"pass_at_{reps}": sum(1 for t in tasks if per_task[t] > 0) / len(tasks) if tasks else None,
        "tasks_all_pass": sum(1 for t in tasks if per_task[t] == len(recs_by_task[t])),
        "tasks_mixed": sum(1 for t in tasks if 0 < per_task[t] < len(recs_by_task[t])),
        "tasks_none_pass": sum(1 for t in tasks if per_task[t] == 0),
        "passes": passes,
        "attempts": len(recs),
        "charged_usd": charged,
        "cost_per_pass_usd": charged / passes if passes else None,
        "refusals": sum(1 for r in recs if (r.get("attempt") or {}).get("status") == "refused"),
        "attempt_status": dict(Counter((r.get("attempt") or {}).get("status") for r in recs)),
        "verdicts": dict(Counter((r.get("verdict") or {}).get("status") for r in recs)),
    }


def _rate_comparison(
    name: str,
    a: ByTask,
    b: ByTask,
    *,
    tasks: Iterable[str],
    reps: int,
    stats: dict[str, Any],
) -> dict[str, Any]:
    run, not_run = _split_run(tasks, a, b, reps, reps)
    ao = {t: [_passed(r) for r in a[t]] for t in run}
    bo = {t: [_passed(r) for r in b[t]] for t in run}
    rates = paired_rates(ao, bo, **stats)
    out = rates.model_dump(mode="json")
    out["name"] = name
    out["not_run"] = not_run
    out["a"] = _side(a, run, reps)
    out["b"] = _side(b, run, reps)
    out["per_task"] = [
        {"task": t, "a": f"{sum(ao[t])}/{len(ao[t])}", "b": f"{sum(bo[t])}/{len(bo[t])}"}
        for t in run
    ]
    av = {t: statistics.fmean(_charged(r) for r in a[t]) for t in run}
    bv = {t: statistics.fmean(_charged(r) for r in b[t]) for t in run}
    out["cost_usd"] = paired_numeric(
        "cost_usd", av, bv, lower_is_better=True, ratio_offset=0.0, **stats
    ).model_dump(mode="json")
    av = {t: statistics.fmean(_usage(r, "llm_calls") for r in a[t]) for t in run}
    bv = {t: statistics.fmean(_usage(r, "llm_calls") for r in b[t]) for t in run}
    out["llm_calls"] = paired_numeric(
        "llm_calls", av, bv, lower_is_better=True, ratio_offset=0.0, **stats
    ).model_dump(mode="json")
    aw = {
        t: statistics.fmean(_num(r["attempt"].get("wall_time_s")) or 0.0 for r in a[t]) for t in run
    }
    bw = {
        t: statistics.fmean(_num(r["attempt"].get("wall_time_s")) or 0.0 for r in b[t]) for t in run
    }
    out["wall_time_s"] = paired_numeric(
        "wall_time_s", aw, bw, lower_is_better=True, ratio_offset=0.0, **stats
    ).model_dump(mode="json")
    return out


def _comparisons(
    spec: StudySpec,
    by: dict[str, ByTask],
    pairs: list[tuple[str, str, str]],
    tasks: list[str],
    metric_names: list[str],
    stats: dict[str, Any],
    robustness: dict[str, ByTask] | None,
) -> list[dict[str, Any]]:
    obj = spec.objective
    reps = spec.held_out.repetitions
    out: list[dict[str, Any]] = []
    for name, a_h, b_h in pairs:
        a, b = by.get(a_h, {}), by.get(b_h, {})
        if obj.kind == "pass_rate":
            out.append(_rate_comparison(name, a, b, tasks=tasks, reps=reps, stats=stats))
            continue
        for m in metric_names:
            out.append(
                _metric_comparison(
                    f"{name}: {m}",
                    a,
                    b,
                    m,
                    tasks=tasks,
                    need_a=reps,
                    need_b=reps,
                    lower_is_better=m not in spec.stats.higher_is_better,
                    ratio_offset=obj.offset if m in obj.metrics else 0.0,
                    stats=stats,
                )
            )
    rob = spec.held_out.robustness
    if robustness is not None and rob is not None and obj.kind == "baseline_ratio":
        need = len(rob.levels) * reps
        for m in obj.metrics:
            out.append(
                _metric_comparison(
                    f"robustness ({rob.knob} averaged over {len(rob.levels)} levels): {m}",
                    robustness["baseline"],
                    robustness["frozen"],
                    m,
                    tasks=tasks,
                    need_a=need,
                    need_b=need,
                    lower_is_better=m not in spec.stats.higher_is_better,
                    ratio_offset=obj.offset,
                    stats=stats,
                )
            )
    return out


def build_results(
    *,
    spec: StudySpec,
    split: Split,
    freeze: dict[str, Any] | None,
    ranking: Ranking,
    smoke: list[dict[str, Any]],
    dev: list[dict[str, Any]],
    held_out: list[dict[str, Any]],
    expected_held_out: list[tuple[str, str, int]],
    provenance: dict[str, Any],
    harnesslab_version: str,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    obj = spec.objective
    stats = {"resamples": spec.stats.resamples, "seed": spec.stats.seed, "level": spec.stats.level}
    metric_names = list(dict.fromkeys([*obj.metrics, *obj.then, *spec.stats.report_metrics]))
    out: dict[str, Any] = {
        "study": spec.name,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "harnesslab_version": harnesslab_version,
        "adapter": {
            "name": spec.adapter,
            "provenance_now": provenance,
            "provenance_at_freeze": (freeze or {}).get("adapter", {}).get("provenance"),
        },
        "split": {
            "file_sha256": split.file_sha256,
            "dev": split.dev,
            "held_out": split.held_out,
        },
        "objective": obj.model_dump(mode="json"),
        "stats": stats,
        "audit": audit or {},
        "freeze": None,
        "smoke": None,
        "dev": None,
        "held_out": None,
    }
    if smoke:
        out["smoke"] = {
            "by_config": {
                h: {"label": rs[0]["config_label"], **_summary(rs, metric_names)}
                for h, rs in _group(smoke).items()
            }
        }
    if dev:
        out["dev"] = {
            "records": len(dev),
            "configurations": len({r["config_hash"] for r in dev}),
            "records_per_stage": dict(Counter(r["stage"] for r in dev)),
            "cost_usd": sum(_usage(r, "cost_usd") for r in dev),
            "charged_usd": sum(_charged(r) for r in dev),
            "cell_seconds": sum(float(r.get("cell_seconds") or 0.0) for r in dev),
            "verdicts": dict(Counter((r.get("verdict") or {}).get("status", "none") for r in dev)),
            "considered_tasks": ranking.task_ids,
            "dropped_tasks": ranking.dropped_tasks,
            "feasible_configurations": sum(1 for s in ranking.scores if s.feasible),
            "ranking_top": [s.model_dump(mode="json") for s in ranking.scores[:25]],
            "baseline": next(
                (
                    s.model_dump(mode="json")
                    for s in ranking.scores
                    if s.config_hash == ranking.baseline_hash
                ),
                None,
            ),
            "by_config_label": {
                label: _summary(rs, metric_names)
                for label, rs in _group_label(dev).items()
                if label in ("baseline", *(c.label for c in spec.held_out.comparators))
            },
        }
    if freeze is not None:
        out["freeze"] = {
            k: freeze[k]
            for k in (
                "frozen_at",
                "freeze_sha256",
                "git_head",
                "baseline",
                "frozen",
                "comparators",
                "spec_sha256",
            )
            if k in freeze
        }
    if freeze is not None and held_out:
        out["held_out"] = _held_out_section(
            spec, freeze, held_out, expected_held_out, metric_names, stats
        )
    return out


def _held_out_section(
    spec: StudySpec,
    freeze: dict[str, Any],
    held_out: list[dict[str, Any]],
    expected: list[tuple[str, str, int]],
    metric_names: list[str],
    stats: dict[str, Any],
) -> dict[str, Any]:
    by = _by_config_task(held_out)
    labels = {r["config_hash"]: r["config_label"] for r in held_out}
    have = {(r["task_id"], r["config_hash"], int(r["repetition"])) for r in held_out}
    missing = [k for k in expected if k not in have]
    unexpected = sorted(have - set(expected))
    base_h = freeze["baseline"]["config_hash"]
    frozen_h = freeze["frozen"]["config_hash"]
    comps = {c["label"]: c["config_hash"] for c in freeze["comparators"]}
    rob = spec.held_out.robustness
    rob_labels = (
        {f"{n}@{rob.knob}={lv}" for n in rob.configs for lv in rob.levels}
        if rob is not None
        else set()
    )
    pairs = [("frozen vs baseline", base_h, frozen_h)]
    pairs += [
        (f"{label} vs baseline", base_h, h) for label, h in comps.items() if label not in rob_labels
    ]
    robustness: dict[str, ByTask] | None = None
    if rob is not None:
        groups = {
            n: [
                comps[f"{n}@{rob.knob}={lv}"]
                for lv in rob.levels
                if f"{n}@{rob.knob}={lv}" in comps
            ]
            for n in rob.configs
        }
        if all(groups.get(n) for n in ("baseline", "frozen")):
            robustness = {
                n: _merge_levels([by.get(h, {}) for h in groups[n]]) for n in ("baseline", "frozen")
            }
    all_tasks = sorted({t for t, _, _ in expected} or {r["task_id"] for r in held_out})
    ho: dict[str, Any] = {
        "complete": not missing,
        "expected_cells": len(expected),
        "recorded_cells": len(have),
        "missing_cells": len(missing),
        "missing_examples": [
            {"task": t, "config_hash": h, "repetition": r} for t, h, r in missing[:50]
        ],
        "unexpected_cells": len(unexpected),
        "records": len(held_out),
        "by_config": {
            h: {
                "label": labels[h],
                **_summary([r for rs in by[h].values() for r in rs], metric_names),
            }
            for h in by
        },
        "failures": [
            {
                "task": r["task_id"],
                "config": r["config_label"],
                "status": r["attempt"]["status"],
                "verdict": (r.get("verdict") or {}).get("status"),
                "reason": (r.get("verdict") or {}).get("reason") or r["attempt"].get("error"),
            }
            for r in held_out
            if not _passed(r)
        ],
        "comparisons": _comparisons(spec, by, pairs, all_tasks, metric_names, stats, robustness),
        "groups": {},
    }
    tags: dict[str, set[str]] = defaultdict(set)
    for r in held_out:
        for tag in r.get("task_tags") or []:
            tags[tag].add(r["task_id"])
    for tag in spec.stats.tag_groups:
        tasks = sorted(tags.get(tag, set()))
        ho["groups"][tag] = {
            "tasks": tasks,
            "comparisons": _comparisons(spec, by, pairs, tasks, metric_names, stats, robustness),
        }
    return ho


def _group(recs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        by[r["config_hash"]].append(r)
    return by


def _group_label(recs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        by[str(r.get("config_label"))].append(r)
    return by


def _merge_levels(per_level: list[ByTask]) -> ByTask:
    """Pool the records of several configurations task by task (their mean becomes the value)."""
    out: ByTask = defaultdict(list)
    for by_task in per_level:
        for t, recs in by_task.items():
            out[t].extend(recs)
    return out

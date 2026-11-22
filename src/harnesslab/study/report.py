"""Compute a study's results from its records: no narrative, only numbers with their provenance.

Held-out comparisons follow the objective's kind:

* ``pass_rate`` studies compare per-task pass rates (paired bootstrap, sign test, exact McNemar on
  majority outcomes), and cost and time per task;
* ``baseline_ratio`` studies compare each metric on the tasks where both sides verified. A task
  where the baseline verified and the other side did not counts as a loss in the sign test
  (``sign_test_p_with_failures``) and is listed; a task where the baseline did not verify is left
  out and listed.

Robustness configurations (one knob replaced by several levels) are compared on per-task means
over those levels.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from harnesslab.study.objective import Ranking
from harnesslab.study.spec import Split, StudySpec
from harnesslab.study.stats import exact_sign_test, paired_numeric, paired_rates


def _passed(r: dict[str, Any]) -> bool:
    return (r.get("verdict") or {}).get("status") == "pass"


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _usage(r: dict[str, Any], key: str) -> float:
    return float(((r.get("attempt") or {}).get("usage") or {}).get(key) or 0.0)


def _summary(recs: list[dict[str, Any]], metric_names: list[str]) -> dict[str, Any]:
    walls = [_num((r.get("attempt") or {}).get("wall_time_s")) for r in recs]
    walls = [w for w in walls if w is not None]
    passes = sum(1 for r in recs if _passed(r))
    cost = sum(_usage(r, "cost_usd") for r in recs)
    out: dict[str, Any] = {
        "records": len(recs),
        "tasks": len({r["task_id"] for r in recs}),
        "verdicts": dict(Counter((r.get("verdict") or {}).get("status", "none") for r in recs)),
        "attempt_status": dict(Counter((r.get("attempt") or {}).get("status") for r in recs)),
        "passes": passes,
        "cost_usd": cost,
        "cost_per_pass_usd": cost / passes if passes else None,
        "llm_calls": int(sum(_usage(r, "llm_calls") for r in recs)),
        "input_tokens": int(sum(_usage(r, "input_tokens") for r in recs)),
        "output_tokens": int(sum(_usage(r, "output_tokens") for r in recs)),
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


def _by_config_task(recs: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    out: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in recs:
        out[r["config_hash"]][r["task_id"]].append(r)
    return out


def _metric_comparison(
    name: str,
    a: dict[str, list[dict[str, Any]]],
    b: dict[str, list[dict[str, Any]]],
    metric: str,
    *,
    lower_is_better: bool,
    stats: dict[str, Any],
) -> dict[str, Any]:
    tasks = sorted(set(a) | set(b))
    a_ok = {t: bool(a.get(t)) and all(_passed(r) for r in a[t]) for t in tasks}
    b_ok = {t: bool(b.get(t)) and all(_passed(r) for r in b[t]) for t in tasks}
    excluded = [t for t in tasks if not a_ok[t]]
    b_failed = [t for t in tasks if a_ok[t] and not b_ok[t]]
    both = [t for t in tasks if a_ok[t] and b_ok[t]]

    def mean_metric(recs: list[dict[str, Any]]) -> float | None:
        vals = [_num((r.get("metrics") or {}).get(metric)) for r in recs]
        return None if any(v is None for v in vals) or not vals else statistics.fmean(vals)  # type: ignore[arg-type]

    av = {t: mean_metric(a[t]) for t in both}
    bv = {t: mean_metric(b[t]) for t in both}
    cmp = paired_numeric(metric, av, bv, lower_is_better=lower_is_better, **stats)
    data = cmp.model_dump(mode="json")
    data.update(
        name=name,
        excluded_baseline_unverified=excluded,
        losses_unverified=b_failed,
        sign_test_p_with_failures=exact_sign_test(cmp.wins, cmp.losses + len(b_failed)),
        per_task=[{"task": t, "a": av[t], "b": bv[t]} for t in both],
    )
    return data


def _rate_comparison(
    name: str,
    a: dict[str, list[dict[str, Any]]],
    b: dict[str, list[dict[str, Any]]],
    *,
    stats: dict[str, Any],
) -> dict[str, Any]:
    tasks = sorted(set(a) & set(b))
    ao = {t: [_passed(r) for r in a[t]] for t in tasks}
    bo = {t: [_passed(r) for r in b[t]] for t in tasks}
    rates = paired_rates(ao, bo, **stats)
    out = rates.model_dump(mode="json")
    out["name"] = name
    out["per_task"] = [
        {"task": t, "a": f"{sum(ao[t])}/{len(ao[t])}", "b": f"{sum(bo[t])}/{len(bo[t])}"}
        for t in tasks
    ]
    for key, label in (("cost_usd", "cost_usd"), ("llm_calls", "llm_calls")):
        av = {t: statistics.fmean(_usage(r, key) for r in a[t]) for t in tasks}
        bv = {t: statistics.fmean(_usage(r, key) for r in b[t]) for t in tasks}
        out[label] = paired_numeric(label, av, bv, lower_is_better=True, **stats).model_dump(
            mode="json"
        )
    aw = {
        t: statistics.fmean(_num(r["attempt"].get("wall_time_s")) or 0.0 for r in a[t])
        for t in tasks
    }
    bw = {
        t: statistics.fmean(_num(r["attempt"].get("wall_time_s")) or 0.0 for r in b[t])
        for t in tasks
    }
    out["wall_time_s"] = paired_numeric(
        "wall_time_s", aw, bw, lower_is_better=True, **stats
    ).model_dump(mode="json")
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
    provenance: dict[str, Any],
    harnesslab_version: str,
) -> dict[str, Any]:
    obj = spec.objective
    stats = {"resamples": spec.stats.resamples, "seed": spec.stats.seed, "level": spec.stats.level}
    metric_names = list(dict.fromkeys([*obj.metrics, *obj.then, *spec.stats.report_metrics]))
    out: dict[str, Any] = {
        "study": spec.name,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "harnesslab_version": harnesslab_version,
        "adapter": {"name": spec.adapter, "provenance": provenance},
        "split": {
            "file_sha256": split.file_sha256,
            "dev": split.dev,
            "held_out": split.held_out,
        },
        "objective": obj.model_dump(mode="json"),
        "stats": stats,
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
    if held_out and freeze is not None:
        by = _by_config_task(held_out)
        labels = {r["config_hash"]: r["config_label"] for r in held_out}
        base_h = freeze["baseline"]["config_hash"]
        frozen_h = freeze["frozen"]["config_hash"]
        comps = {c["label"]: c["config_hash"] for c in freeze["comparators"]}
        ho: dict[str, Any] = {
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
            "comparisons": [],
        }
        pairs = [("frozen vs baseline", base_h, frozen_h)]
        rob = spec.held_out.robustness
        rob_labels = (
            {f"{n}@{rob.knob}={lv}" for n in rob.configs for lv in rob.levels}
            if rob is not None
            else set()
        )
        pairs += [
            (f"{label} vs baseline", base_h, h)
            for label, h in comps.items()
            if label not in rob_labels
        ]
        for name, a_h, b_h in pairs:
            if a_h not in by or b_h not in by:
                continue
            if obj.kind == "pass_rate":
                ho["comparisons"].append(_rate_comparison(name, by[a_h], by[b_h], stats=stats))
            else:
                for m in metric_names:
                    lower = m not in spec.stats.higher_is_better
                    ho["comparisons"].append(
                        _metric_comparison(
                            f"{name}: {m}", by[a_h], by[b_h], m, lower_is_better=lower, stats=stats
                        )
                    )
        if rob is not None and obj.kind == "baseline_ratio":
            groups: dict[str, list[str]] = {}
            for n in rob.configs:
                groups[n] = [
                    comps[f"{n}@{rob.knob}={lv}"]
                    for lv in rob.levels
                    if f"{n}@{rob.knob}={lv}" in comps
                ]
            if all(groups.get(n) for n in ("baseline", "frozen")):
                merged = {
                    n: _merge_levels([by[h] for h in groups[n] if h in by])
                    for n in ("baseline", "frozen")
                }
                for m in obj.metrics:
                    ho["comparisons"].append(
                        _metric_comparison(
                            f"robustness ({rob.knob} averaged over {len(rob.levels)} levels): {m}",
                            merged["baseline"],
                            merged["frozen"],
                            m,
                            lower_is_better=m not in spec.stats.higher_is_better,
                            stats=stats,
                        )
                    )
        out["held_out"] = ho
    return out


def _group(recs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        by[r["config_hash"]].append(r)
    return by


def _merge_levels(
    per_level: list[dict[str, list[dict[str, Any]]]],
) -> dict[str, list[dict[str, Any]]]:
    """Pool the records of several configurations task by task (their mean becomes the value)."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for by_task in per_level:
        for t, recs in by_task.items():
            out[t].extend(recs)
    return out

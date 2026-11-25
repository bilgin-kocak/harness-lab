"""The registered analysis of the Haiku feedback study (REGISTRATION.md, section 6).

Written and committed before any held-out data exists. Usage::

    python studies/2026-11-math-feedback-haiku/analyze.py [--out analysis.json]

Reads the held-out records (``held_out/attempts.jsonl`` or ``.jsonl.gz``), ``freeze.json`` and
``split.json``, calls nothing and changes nothing. Arms, by their labels in the freeze:

* ``baseline`` (B): one call;
* ``F``: up to 3 feedback rounds, each showing the failed proof and Lean's report;
* ``R4``: up to 4 fresh tries, no feedback;
* ``P``: as F, but a fixed sentence replaces Lean's report.

The unit is a problem's verified rate: verified attempts / repetitions. An attempt is verified when
the independent verifier accepted its output proof, whatever the attempt's status.

The output has these sections:

1. **Completeness.** The registered analysis uses the problems with every repetition of every arm.
2. **Primary.** F vs R4:
   * the mean of d = rate_F - rate_R4;
   * its 95% percentile bootstrap interval;
   * the two-sided paired sign-flip test at alpha 0.05;
   * the 90% interval used for the equivalence reading (+-0.03);
   * the reading the registration fixed.
3. **Secondary.** F vs P and P vs R4, with Holm-adjusted sign-flip p-values (family of two).
4. **Descriptive.** R4 - B, F - B and P - B, with intervals, sign-flip p-values (descriptive only)
   and every problem whose rates differ.
5. **Diagnostics.**
   * first-call parity among F, R4 and P (identical first prompt);
   * rescue after a failed first call;
   * drift by time quarter;
   * statuses, refusals, token-cap stops, served-model mismatches and harness errors.
6. **Sensitivity of the primary contrast.**
   * without the split's ``sensitivity_exclusions``;
   * without problems where any verdict timed out;
   * the exact sign test.
7. **Cost.** By arm, and the F/R4 ratio of cost per verified proof, with a bootstrap over problems.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from harnesslab.study.stats import (  # noqa: E402
    bootstrap_mean_diff,
    exact_sign_test,
    sign_flip_test,
)
from harnesslab.study.store import StudyStore  # noqa: E402

ARMS = ("baseline", "F", "R4", "P")
MODEL = "claude-haiku-5-5"
SEED = 20261125
BOOT = 10_000
FLIPS = 100_000
ALPHA = 0.05
SESOI = 0.03  # smallest effect of interest, in verified-rate points (3 percentage points)


def _round1_passed(r: dict) -> bool:
    rounds = ((r.get("attempt") or {}).get("details") or {}).get("rounds") or []
    if not rounds:
        return False
    inloop = rounds[0].get("inloop") or {}
    return inloop.get("status") == "pass"


def _timed_out(r: dict) -> bool:
    checks = (((r.get("verdict") or {}).get("details") or {}).get("checks")) or {}
    return bool(checks.get("timed_out"))


def _cost(r: dict) -> float:
    if r.get("charged_usd") is not None:
        return float(r["charged_usd"])
    return float((r.get("metrics") or {}).get("cost_usd") or 0.0)


def _contrast(name: str, rates: dict, a: str, b: str, tasks: list[str], *, test: bool) -> dict:
    """b - a on per-problem rates over ``tasks``."""
    d = [rates[b][t] - rates[a][t] for t in tasks]
    ci95 = bootstrap_mean_diff(d, resamples=BOOT, seed=SEED, level=0.95)
    ci90 = bootstrap_mean_diff(d, resamples=BOOT, seed=SEED, level=0.90)
    wins = sum(1 for x in d if x > 1e-9)
    losses = sum(1 for x in d if x < -1e-9)
    out = {
        "contrast": name,
        "n_problems": len(tasks),
        "rate_a": statistics.fmean(rates[a][t] for t in tasks),
        "rate_b": statistics.fmean(rates[b][t] for t in tasks),
        "estimate": ci95.estimate,
        "ci95": [ci95.low, ci95.high],
        "ci90": [ci90.low, ci90.high],
        "wins_losses_ties": [wins, losses, len(d) - wins - losses],
        "sign_flip_p": sign_flip_test(d, resamples=FLIPS, seed=SEED),
        "sign_test_p": exact_sign_test(wins, losses),
        "differing": sorted(
            [t, round(rates[a][t], 3), round(rates[b][t], 3)]
            for t in tasks
            if abs(rates[b][t] - rates[a][t]) > 1e-9
        ),
        "role": "test" if test else "descriptive",
    }
    p = out["sign_flip_p"]
    print(
        f"{name}: n={len(tasks)} rates {out['rate_a']:.3f} -> {out['rate_b']:.3f}, "
        f"{out['estimate']:+.4f} 95% [{ci95.low:+.4f}, {ci95.high:+.4f}] 90% [{ci90.low:+.4f}, "
        f"{ci90.high:+.4f}], {wins}/{losses}/{len(d) - wins - losses}, sign-flip p = "
        f"{'n/a' if p is None else f'{p:.5f}'}{'' if test else ' (descriptive)'}"
    )
    return out


def reading(primary: dict) -> str:
    """The registered reading of the primary contrast (REGISTRATION.md, section 6.2)."""
    p = primary["sign_flip_p"]
    lo, hi = primary["ci90"]
    if p is not None and p < ALPHA:
        direction = "F above R4" if primary["estimate"] > 0 else "F below R4"
        return f"difference: {direction}"
    if -SESOI < lo and hi < SESOI:
        return f"no difference of {SESOI:.2f} or more (90% interval within +-{SESOI:.2f})"
    return "inconclusive"


def holm(pvals: dict[str, float | None]) -> dict[str, float | None]:
    items = sorted(((p, k) for k, p in pvals.items() if p is not None))
    m, out, running = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return {k: out.get(k) for k in pvals}


def _cluster_boot(tasks: list[str], stat, *, seed: int = SEED) -> list[float]:
    rng = random.Random(seed)
    vals = []
    for _ in range(BOOT):
        sample = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        v = stat(sample)
        if v is not None and math.isfinite(v):
            vals.append(v)
    vals.sort()
    if not vals:
        return [float("nan"), float("nan")]
    return [vals[int(0.025 * len(vals))], vals[min(len(vals) - 1, int(0.975 * len(vals)))]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--study-dir", type=Path, default=HERE)
    args = parser.parse_args()
    study_dir = args.study_dir
    freeze = json.loads((study_dir / "freeze.json").read_text(encoding="utf-8"))
    split = json.loads((study_dir / "split.json").read_text(encoding="utf-8"))
    held = [e["id"] for e in split["held_out"]]
    labels = {freeze["baseline"]["config_hash"]: "baseline", freeze["frozen"]["config_hash"]: "F"}
    labels |= {c["config_hash"]: c["label"] for c in freeze["comparators"]}
    if sorted(labels.values()) != sorted(ARMS):
        raise SystemExit(f"the freeze's arms are {sorted(labels.values())}, expected {ARMS}")
    reps = int(freeze["held_out_spec"]["repetitions"])
    store = StudyStore(study_dir)
    all_records = store.records("held_out")
    records = store.effective("held_out")
    out: dict = {"study": split["study"], "freeze_sha256": freeze["freeze_sha256"]}

    # 1. completeness
    by: dict[str, dict[str, list[dict]]] = {a: defaultdict(list) for a in ARMS}
    for r in records:
        by[labels[r["config_hash"]]][r["task_id"]].append(r)
    complete = [t for t in held if all(len(by[a].get(t, [])) == reps for a in ARMS)]
    out["completeness"] = {
        "expected_cells": len(held) * reps * len(ARMS),
        "valid_records": len(records),
        "harness_error_records": sum(1 for r in all_records if r.get("harness_error")),
        "complete_problems": len(complete),
        "incomplete_problems": sorted(set(held) - set(complete)),
    }
    print(f"REGISTERED ANALYSIS (REGISTRATION.md, section 6). {out['completeness']}")
    rates = {a: {t: sum(bool(r["metrics"].get("verified")) for r in by[a][t]) / reps
                 for t in complete} for a in ARMS}  # fmt: skip

    # 2. primary
    primary = _contrast("PRIMARY F - R4", rates, "R4", "F", complete, test=True)
    primary["reading"] = reading(primary)
    print(f"  registered reading: {primary['reading']}")
    out["primary"] = primary

    # 3. secondary (Holm, family of two)
    sec = {
        "F - P": _contrast("SECONDARY F - P", rates, "P", "F", complete, test=True),
        "P - R4": _contrast("SECONDARY P - R4", rates, "R4", "P", complete, test=True),
    }
    adjusted = holm({k: v["sign_flip_p"] for k, v in sec.items()})
    for k, v in sec.items():
        v["holm_p"] = adjusted[k]
        v["significant_holm"] = adjusted[k] is not None and adjusted[k] < ALPHA
        print(f"  {k}: Holm-adjusted p = {adjusted[k]}")
    out["secondary"] = sec

    # 4. descriptive
    out["descriptive"] = [
        _contrast(f"{b} - baseline", rates, "baseline", b, complete, test=False)
        for b in ("R4", "F", "P")
    ]

    # 5. diagnostics
    first = {a: {t: sum(_round1_passed(r) for r in by[a][t]) / reps for t in complete}
             for a in ("F", "R4", "P")}  # fmt: skip
    parity = {}
    for a, b in (("R4", "F"), ("P", "F"), ("R4", "P")):
        d = [first[b][t] - first[a][t] for t in complete]
        p = sign_flip_test(d, resamples=FLIPS, seed=SEED)
        parity[f"{b} - {a}"] = {"mean": statistics.fmean(d), "sign_flip_p": p}
    out["first_call_parity"] = {
        "rates": {a: statistics.fmean(first[a].values()) for a in first},
        "pairs": parity,
        "flag": any(
            v["sign_flip_p"] is not None and v["sign_flip_p"] < ALPHA for v in parity.values()
        ),  # fmt: skip
    }
    print(f"first-call parity (identical first prompt): {out['first_call_parity']}")

    def rescue_share(arm: str, tasks: list[str]) -> float | None:
        failed = [r for t in tasks for r in by[arm][t] if not _round1_passed(r)]
        return (
            sum(bool(r["metrics"].get("verified")) for r in failed) / len(failed)
            if failed
            else None
        )

    rescue = {}
    for arm in ("F", "R4", "P"):
        failed = [r for t in complete for r in by[arm][t] if not _round1_passed(r)]
        rescue[arm] = {"failed_first_call": len(failed),
                       "verified_after": sum(bool(r["metrics"].get("verified")) for r in failed)}  # fmt: skip
    for a, b in (("R4", "F"), ("P", "F")):

        def diff(sample, a=a, b=b):
            x, y = rescue_share(b, sample), rescue_share(a, sample)
            return None if x is None or y is None else x - y

        rescue[f"{b} - {a}"] = {"estimate": diff(complete), "ci95": _cluster_boot(complete, diff)}
    out["rescue_after_failed_first_call"] = rescue
    print(f"rescue after a failed first call: {rescue}")

    quarters: dict[str, list] = {}
    timed = sorted(records, key=lambda r: r.get("started_at") or "")
    for q in range(4):
        chunk = timed[q * len(timed) // 4 : (q + 1) * len(timed) // 4]
        share = {}
        for a in ARMS:
            xs = [
                bool(r["metrics"].get("verified")) for r in chunk if labels[r["config_hash"]] == a
            ]
            share[a] = sum(xs) / len(xs) if xs else None
        quarters[f"q{q + 1}"] = share
    out["drift_by_time_quarter"] = quarters
    print(f"verified share by time quarter: {quarters}")

    status = Counter(
        (labels[r["config_hash"]], (r.get("attempt") or {}).get("status")) for r in records
    )
    mismatched = sum(
        1
        for r in records
        for c in ((r.get("attempt") or {}).get("details") or {}).get("calls") or []
        if c.get("model") != MODEL
    )
    out["integrity"] = {
        "status": {f"{a}/{s}": n for (a, s), n in sorted(status.items())},
        "refusals": {a: sum(int(r["metrics"].get("refusals") or 0) for t in complete for r in by[a][t]) for a in ARMS},
        "max_tokens_stops": {a: sum(int(r["metrics"].get("max_tokens_stops") or 0) for t in complete for r in by[a][t]) for a in ARMS},
        "calls_served_by_another_model": mismatched,
    }  # fmt: skip
    print(f"integrity: {out['integrity']}")

    # 6. sensitivity of the primary contrast
    excl = set(split.get("sensitivity_exclusions", {}).get("ids", []))
    no_defect = [t for t in complete if t not in excl]
    timeouts = {t for t in complete for a in ARMS for r in by[a][t] if _timed_out(r)}
    no_timeout = [t for t in complete if t not in timeouts]
    out["sensitivity"] = {
        "without_sensitivity_exclusions": _contrast(
            "F - R4 without the defect-list problems", rates, "R4", "F", no_defect, test=False
        ),
        "without_timeout_problems": _contrast(
            "F - R4 without problems with a verifier timeout",
            rates,
            "R4",
            "F",
            no_timeout,
            test=False,
        ),
        "timeout_problems": sorted(timeouts),
        "exact_sign_test_p": primary["sign_test_p"],
    }

    # 7. cost
    def totals(arm: str, tasks: list[str]) -> tuple[float, int, int]:
        rs = [r for t in tasks for r in by[arm][t]]
        return (
            sum(_cost(r) for r in rs),
            sum(bool(r["metrics"].get("verified")) for r in rs),
            len(rs),
        )

    cost = {}
    for a in ARMS:
        c, v, n = totals(a, complete)
        calls = sum(int(r["metrics"].get("llm_calls") or 0) for t in complete for r in by[a][t])
        cost[a] = {"usd": round(c, 6), "attempts": n, "verified": v,
                   "usd_per_attempt": c / n if n else None,
                   "usd_per_verified": c / v if v else None,
                   "calls_per_attempt": calls / n if n else None}  # fmt: skip

    def ratio(sample):
        cf, vf, _ = totals("F", sample)
        cr, vr, _ = totals("R4", sample)
        return (cf / vf) / (cr / vr) if vf and vr and cr else None

    cost["F/R4 cost per verified proof"] = {
        "estimate": ratio(complete),
        "ci95": _cluster_boot(complete, ratio),
    }
    cost["spend_all_phases_usd"] = sum(_cost(r) for ph in ("smoke", "dev", "held_out") for r in store.records(ph))  # fmt: skip
    out["cost"] = cost
    print(f"cost: {json.dumps(cost, indent=1)}")

    if args.out:
        args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The registered analysis of the Haiku feedback study (REGISTRATION.md, section 6).

Written and committed before any held-out data exists. Usage::

    python studies/2026-11-math-feedback-haiku/analyze.py [--out analysis.json] [--study-dir DIR]

Reads the held-out records, ``freeze.json`` and ``split.json``. It calls nothing and changes
nothing.

**Arms** (labels from the freeze):

* B (``baseline``): 1 call.
* F: up to 3 feedback rounds, each showing the failed proof and Lean's report.
* R4: up to 4 fresh tries.
* P: as F, but a fixed sentence replaces Lean's report.

**Unit.** A problem's verified rate in an arm: its verified attempts over the repetitions that have
a valid record in all four arms. That is all 5 repetitions on a complete run (REGISTRATION.md
6.1). An attempt is verified when the independent verifier accepted its output proof, whatever
the attempt's status. The registered results are this script's output. The protocol's own
``results.json`` and report are descriptive.
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
ALPHA = 0.05
SESOI = 0.03  # 3 percentage points of the overall verified rate
LATTICE = 60  # rates over 1 to 5 repetitions are multiples of 1/60: the sign-flip p is exact
TOL = 1e-9
PER_ATTEMPT_KEYS = (
    "attempts_capped",
    "attempts_error",
    "verifier_timeouts",
    "rounds_with_dropped_context",
    "inloop_timeouts",
    "harness_error_records",
)


# --------------------------------------------------------------------------- record fields


def _verified(r: dict) -> bool:
    return bool((r.get("metrics") or {}).get("verified"))


def _details(r: dict) -> dict:
    return (r.get("attempt") or {}).get("details") or {}


def _status(r: dict) -> str | None:
    return (r.get("attempt") or {}).get("status")


def _round1(r: dict) -> dict:
    rounds = _details(r).get("rounds") or []
    return rounds[0] if rounds else {}


def _round1_passed(r: dict) -> bool:
    return (_round1(r).get("inloop") or {}).get("status") == "pass"


def _round1_failure_class(r: dict) -> str:
    """Why round 1 did not pass in the loop (identical first request in F, R4 and P)."""
    first = _round1(r)
    if first.get("outcome") != "proof":
        return "no proof"
    reason = str((first.get("inloop") or {}).get("reason") or "").lower()
    if reason == "timeout":
        return "timeout"
    if "unknown identifier" in reason or "unknown constant" in reason:
        return "unknown name"
    return "other Lean error or rejection"


def _verdict_checks(r: dict) -> dict:
    return (((r.get("verdict") or {}).get("details") or {}).get("checks")) or {}


def _verifier_seconds(r: dict) -> float:
    durations = ((r.get("verdict") or {}).get("details") or {}).get("durations") or {}
    return float(durations.get("lean_s") or 0.0)


def _cost(r: dict) -> float:
    if r.get("charged_usd") is not None:
        return float(r["charged_usd"])
    return float((r.get("metrics") or {}).get("cost_usd") or 0.0)


# --------------------------------------------------------------------------- statistics


def _p(d: list[float]) -> float:
    """The registered two-sided sign-flip p-value: exact on the 1/60 lattice; 1 if none differ."""
    p = sign_flip_test(d, lattice=LATTICE)
    return 1.0 if p is None else p


def holm(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted((p, k) for k, p in pvals.items())
    out, running, m = {}, 0.0, len(items)
    for i, (p, k) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out


def three_way(p: float, ci90: list[float] | None, estimate: float | None) -> str:
    """The registered reading (REGISTRATION.md 6.2), on a given (possibly adjusted) p-value."""
    if estimate is None or ci90 is None:
        return "no data"
    if p < ALPHA:
        return f"difference ({'positive' if estimate > 0 else 'negative'})"
    if -SESOI < ci90[0] and ci90[1] < SESOI:
        return f"no difference of {SESOI:.2f} or more in the overall verified rate"
    return "inconclusive"


def _cluster_boot(tasks: list[str], stat) -> list[float]:
    """95% percentile bootstrap over problems (10,000 resamples, seed 20261125); resamples where
    the statistic is undefined are left out."""
    if not tasks:
        return [float("nan"), float("nan")]
    rng = random.Random(SEED)
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


def _contrast(name: str, rates: dict, a: str, b: str, tasks: list[str], role: str) -> dict:
    """b - a on per-problem rates over ``tasks``."""
    d = [rates[b][t] - rates[a][t] for t in tasks]
    ci95 = bootstrap_mean_diff(d, resamples=BOOT, seed=SEED, level=0.95)
    ci90 = bootstrap_mean_diff(d, resamples=BOOT, seed=SEED, level=0.90)
    wins = sum(1 for x in d if x > TOL)
    losses = sum(1 for x in d if x < -TOL)
    out = {
        "contrast": name,
        "role": role,
        "n_problems": len(tasks),
        "rate_a": statistics.fmean(rates[a][t] for t in tasks) if tasks else None,
        "rate_b": statistics.fmean(rates[b][t] for t in tasks) if tasks else None,
        "estimate": ci95.estimate if ci95 else None,
        "ci95": [ci95.low, ci95.high] if ci95 else None,
        "ci90": [ci90.low, ci90.high] if ci90 else None,
        "wins_losses_ties": [wins, losses, len(d) - wins - losses],
        "sign_flip_p": _p(d) if d else None,
        "sign_test_p": exact_sign_test(wins, losses),
        "differing": sorted(
            [t, round(rates[a][t], 4), round(rates[b][t], 4)]
            for t in tasks
            if abs(rates[b][t] - rates[a][t]) > TOL
        ),
    }
    if tasks:
        print(
            f"{name} [{role}]: n={len(tasks)} rates {out['rate_a']:.4f} -> {out['rate_b']:.4f}, "
            f"{out['estimate']:+.4f} 95% [{ci95.low:+.4f}, {ci95.high:+.4f}] 90% "
            f"[{ci90.low:+.4f}, {ci90.high:+.4f}], {wins}/{losses}/{len(d) - wins - losses}, "
            f"sign-flip p = {out['sign_flip_p']:.6f}"
        )
    return out


# --------------------------------------------------------------------------- the analysis


class Data:
    """The held-out records, by arm, problem and repetition, with the matched repetitions."""

    def __init__(self, study_dir: Path) -> None:
        self.freeze = json.loads((study_dir / "freeze.json").read_text(encoding="utf-8"))
        self.split = json.loads((study_dir / "split.json").read_text(encoding="utf-8"))
        self.held = [e["id"] for e in self.split["held_out"]]
        f = self.freeze
        self.labels = {f["baseline"]["config_hash"]: "baseline", f["frozen"]["config_hash"]: "F"}
        self.labels |= {c["config_hash"]: c["label"] for c in f["comparators"]}
        if sorted(self.labels.values()) != sorted(ARMS):
            raise SystemExit(f"the freeze's arms are {sorted(self.labels.values())}, not {ARMS}")
        self.reps = int(f["held_out_spec"]["repetitions"])
        self.store = StudyStore(study_dir)
        self.all_records = self.store.records("held_out")
        self.records = self.store.effective("held_out")
        self.cell = {
            (self.labels[r["config_hash"]], r["task_id"], int(r["repetition"])): r
            for r in self.records
        }
        self.common = {
            t: [k for k in range(self.reps) if all((a, t, k) in self.cell for a in ARMS)]
            for t in self.held
        }
        self.usable = [t for t in self.held if self.common[t]]
        self.complete = [t for t in self.held if len(self.common[t]) == self.reps]

    def att(self, arm: str, task: str) -> list[dict]:
        return [self.cell[(arm, task, k)] for k in self.common[task]]

    def rates(self, value=_verified, arms=ARMS) -> dict[str, dict[str, float]]:
        return {
            a: {t: statistics.fmean(value(r) for r in self.att(a, t)) for t in self.usable}
            for a in arms
        }

    def arm_records(self, arm: str) -> list[dict]:
        return [r for r in self.records if self.labels[r["config_hash"]] == arm]


def data_section(d: Data) -> dict:
    out = {
        "expected_cells": len(d.held) * d.reps * len(ARMS),
        "valid_records": len(d.records),
        "harness_error_records": sum(1 for r in d.all_records if r.get("harness_error")),
        "usable_problems": len(d.usable),
        "complete_problems": len(d.complete),
        "problems_without_a_matched_repetition": sorted(set(d.held) - set(d.usable)),
        "missing_cells": sorted(
            [a, t, k]
            for a in ARMS
            for t in d.held
            for k in range(d.reps)
            if (a, t, k) not in d.cell
        ),
    }
    shown = {k: v for k, v in out.items() if k != "missing_cells"}
    print(f"REGISTERED ANALYSIS (REGISTRATION.md 6). data: {shown}")
    return out


def hypotheses(d: Data, rates: dict) -> tuple[dict, dict, str]:
    primary = _contrast("H1 F - R4", rates, "R4", "F", d.usable, "primary")
    primary["reading"] = three_way(primary["sign_flip_p"], primary["ci90"], primary["estimate"])
    primary["r4_unverified_share"] = (
        1 - primary["rate_a"] if primary["rate_a"] is not None else None
    )
    print(
        f"  H1 reading: {primary['reading']}; R4 unverified share {primary['r4_unverified_share']}"
    )
    sec = {
        "H2 F - P": _contrast("H2 F - P", rates, "P", "F", d.usable, "secondary"),
        "H3 P - R4": _contrast("H3 P - R4", rates, "R4", "P", d.usable, "secondary"),
    }
    adjusted = holm({k: (v["sign_flip_p"] if v["sign_flip_p"] is not None else 1.0)
                     for k, v in sec.items()})  # fmt: skip
    for k, v in sec.items():
        v["holm_p"] = adjusted[k]
        v["reading"] = three_way(adjusted[k], v["ci90"], v["estimate"])
        print(f"  {k}: Holm p = {adjusted[k]:.6f}, reading: {v['reading']}")
    h1 = primary["reading"].startswith("difference")
    h2 = sec["H2 F - P"]["reading"].startswith("difference")
    if h1 and not h2:
        attribution = (
            "H1 shows a difference and H2 does not: the difference from fresh tries is not "
            "attributed to Lean's report; it may come from seeing the failed attempt (H3)."
        )
    elif h1:
        attribution = "H1 and H2 both show a difference: Lean's report itself adds to the effect."
    else:
        attribution = "H1 shows no difference at alpha 0.05: nothing is attributed."
    print(f"  attribution rule: {attribution}")
    return primary, sec, attribution


def descriptive(d: Data, rates: dict) -> dict:
    strata: dict[str, list[str]] = {"baseline 1": [], "baseline between": [], "baseline 0": []}
    for t in d.usable:
        b = rates["baseline"][t]
        key = "baseline 1" if b > 1 - TOL else "baseline 0" if b < TOL else "baseline between"
        strata[key].append(t)

    def headroom(sample: list[str]) -> float | None:
        f = statistics.fmean(rates["F"][t] for t in sample)
        r = statistics.fmean(rates["R4"][t] for t in sample)
        return (f - r) / (1 - r) if r < 1 - TOL else None

    first = d.rates(_round1_passed, ("F", "R4", "P"))
    post = {a: {t: rates[a][t] - first[a][t] for t in d.usable} for a in ("F", "R4", "P")}
    return {
        "vs_baseline": [
            _contrast(f"{b} - baseline", rates, "baseline", b, d.usable, "descriptive")
            for b in ("R4", "F", "P")
        ],
        "F - R4 by baseline stratum": {
            k: _contrast(f"F - R4 where {k}", rates, "R4", "F", ts, "descriptive")
            for k, ts in strata.items()
            if ts
        },
        "headroom_scaled F - R4": {
            "estimate": headroom(d.usable) if d.usable else None,
            "ci95": _cluster_boot(d.usable, headroom),
        },
        "post_first_call_gain F - R4": _contrast(
            "post-first-call gain F - R4", post, "R4", "F", d.usable, "descriptive"
        ),
    }


def parity(d: Data) -> dict:
    first = d.rates(_round1_passed, ("F", "R4", "P"))
    raw, means = {}, {}
    for a, b in (("R4", "F"), ("P", "F"), ("R4", "P")):
        diffs = [first[b][t] - first[a][t] for t in d.usable]
        raw[f"{b} - {a}"] = _p(diffs)
        means[f"{b} - {a}"] = statistics.fmean(diffs) if diffs else None
    adjusted = holm(raw)
    out = {
        "round1_pass_rate": {
            a: (statistics.fmean(first[a].values()) if first[a] else None) for a in first
        },
        "pairs": {k: {"mean": means[k], "sign_flip_p": raw[k], "holm_p": adjusted[k]} for k in raw},
        "flag": any(p < ALPHA for p in adjusted.values()),
        "rule": "the flag changes no reading of H1-H3; it is reported next to them",
    }
    print(f"first-call parity: {out}")
    return out


def rescue(d: Data) -> dict:
    def share(arm: str, sample: list[str]) -> float | None:
        failed = [r for t in sample for r in d.att(arm, t) if not _round1_passed(r)]
        return statistics.fmean(_verified(r) for r in failed) if failed else None

    out: dict = {}
    for arm in ("F", "R4", "P"):
        failed = [r for t in d.usable for r in d.att(arm, t) if not _round1_passed(r)]
        classes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in failed:
            classes[_round1_failure_class(r)][0] += 1
            classes[_round1_failure_class(r)][1] += _verified(r)
        out[arm] = {
            "failed_first_call": len(failed),
            "verified_after": sum(_verified(r) for r in failed),
            "share": share(arm, d.usable),
            "by_failure_class": {
                k: {"failed": v[0], "verified_after": v[1]} for k, v in sorted(classes.items())
            },
        }
    for a, b in (("R4", "F"), ("P", "F")):

        def diff(sample, a=a, b=b):
            x, y = share(b, sample), share(a, sample)
            return None if x is None or y is None else x - y

        out[f"{b} - {a}"] = {
            "estimate": diff(d.usable) if d.usable else None,
            "ci95": _cluster_boot(d.usable, diff),
        }
    print(f"rescue after a failed first call: {json.dumps(out)}")
    return out


def drift(d: Data) -> dict:
    timed = sorted(d.records, key=lambda r: r.get("started_at") or "")
    out = {}
    for q in range(4):
        chunk = timed[q * len(timed) // 4 : (q + 1) * len(timed) // 4]
        out[f"q{q + 1}"] = {}
        for a in ARMS:
            xs = [_verified(r) for r in chunk if d.labels[r["config_hash"]] == a]
            out[f"q{q + 1}"][a] = statistics.fmean(xs) if xs else None
    return out


def integrity(d: Data) -> tuple[dict, list[str]]:
    out: dict = {}
    for a in ARMS:
        rs = d.arm_records(a)
        n = len(rs) or 1
        calls = [c for r in rs for c in _details(r).get("calls") or []]
        rounds = [rd for r in rs for rd in _details(r).get("rounds") or []]
        row = {
            "valid_records": len(rs),
            "status": dict(Counter(_status(r) for r in rs)),
            "stop": dict(Counter(_details(r).get("stop") for r in rs)),
            "attempts_capped": sum(1 for r in rs if _status(r) == "capped"),
            "attempts_error": sum(1 for r in rs if _status(r) == "error"),
            "harness_error_records": sum(
                1
                for r in d.all_records
                if r.get("harness_error") and d.labels.get(r.get("config_hash")) == a
            ),
            "refusals": sum(int(r["metrics"].get("refusals") or 0) for r in rs),
            "max_tokens_stops": sum(int(r["metrics"].get("max_tokens_stops") or 0) for r in rs),
            "calls_served_by_another_model": sum(
                1 for c in calls if c.get("model") is not None and c["model"] != MODEL
            ),
            "calls_without_response": sum(1 for c in calls if c.get("model") is None),
            "attempts_invalidated_served_model": sum(
                1 for r in rs if _details(r).get("stop") == "error:served_model"
            ),
            "rounds_with_dropped_context": sum(1 for rd in rounds if rd.get("dropped")),
            "inloop_timeouts": sum(
                1 for rd in rounds if (rd.get("inloop") or {}).get("reason") == "timeout"
            ),
            "verifier_timeouts": sum(1 for r in rs if _verdict_checks(r).get("timed_out")),
            "verified_with_verifier_over_120s": sum(
                1 for r in rs if _verified(r) and _verifier_seconds(r) > 120
            ),
        }
        row["per_attempt"] = {k: row[k] / n for k in PER_ATTEMPT_KEYS}
        out[a] = row
    asym = []
    for x, y in (("F", "R4"), ("F", "P")):
        for key in PER_ATTEMPT_KEYS:
            vx, vy = out[x]["per_attempt"][key], out[y]["per_attempt"][key]
            if abs(vx - vy) > 0.01:
                asym.append(f"{key}: {x} {vx:.4f} vs {y} {vy:.4f}")
    print(f"integrity asymmetries over 1 point (reported next to H1/H2): {asym or 'none'}")
    return out, asym


def sensitivity(d: Data, rates: dict, primary: dict) -> dict:
    excl = set(d.split.get("sensitivity_exclusions", {}).get("ids", []))
    timeouts = {
        t
        for t in d.usable
        for a in ARMS
        for r in d.att(a, t)
        if _verdict_checks(r).get("timed_out")
    }
    return {
        "without_sensitivity_exclusions": _contrast(
            "H1 without the defect-list problems",
            rates,
            "R4",
            "F",
            [t for t in d.usable if t not in excl],
            "sensitivity",
        ),
        "without_timeout_problems": _contrast(
            "H1 without problems with a verifier timeout",
            rates,
            "R4",
            "F",
            [t for t in d.usable if t not in timeouts],
            "sensitivity",
        ),
        "complete_case": _contrast(
            "H1 on complete problems only", rates, "R4", "F", d.complete, "sensitivity"
        ),
        "timeout_problems": sorted(timeouts),
        "exact_sign_test_p": primary["sign_test_p"],
        "rule": "reported next to H1; none replaces the primary",
    }


def cost(d: Data) -> dict:
    def totals(arm: str, sample: list[str]) -> tuple[float, int, int, int, int]:
        rs = [r for t in sample for r in d.att(arm, t)]
        return (
            sum(_cost(r) for r in rs),
            sum(_verified(r) for r in rs),
            len(rs),
            sum(int(r["metrics"].get("llm_calls") or 0) for r in rs),
            sum(int(r["metrics"].get("output_tokens") or 0) for r in rs),
        )

    out: dict = {}
    for a in ARMS:
        c, v, n, calls, tokens = totals(a, d.usable)
        out[a] = {
            "usd": round(c, 6),
            "attempts": n,
            "verified": v,
            "output_tokens": tokens,
            "usd_per_attempt": c / n if n else None,
            "usd_per_verified": c / v if v else None,
            "calls_per_attempt": calls / n if n else None,
        }

    def ratio(sample: list[str]) -> float | None:
        cf, vf, *_ = totals("F", sample)
        cr, vr, *_ = totals("R4", sample)
        return (cf / vf) / (cr / vr) if vf and vr and cr else None

    out["F/R4 cost per verified proof"] = {
        "estimate": ratio(d.usable) if d.usable else None,
        "ci95": _cluster_boot(d.usable, ratio),
    }
    phases = ("smoke", "dev", "held_out")
    spent = sum(_cost(r) for ph in phases for r in d.store.records(ph))
    dangling = sum(float(e["usd"]) for ph in phases for e in d.store.dangling_reservations(ph))
    out["spend_records_usd"] = spent
    out["spend_with_interrupted_cells_usd"] = spent + dangling
    print(f"cost: {json.dumps(out, indent=1)}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--study-dir", type=Path, default=HERE)
    args = parser.parse_args()
    d = Data(args.study_dir)
    out: dict = {"study": d.split["study"], "freeze_sha256": d.freeze["freeze_sha256"]}
    out["data"] = data_section(d)
    rates = d.rates()
    out["primary"], out["secondary"], out["attribution"] = hypotheses(d, rates)
    out["descriptive"] = descriptive(d, rates)
    out["first_call_parity"] = parity(d)
    out["rescue_after_failed_first_call"] = rescue(d)
    out["drift_by_time_quarter"] = drift(d)
    out["integrity"], out["integrity_asymmetries_over_1_point"] = integrity(d)
    out["sensitivity"] = sensitivity(d, rates, out["primary"])
    out["cost"] = cost(d)
    if args.out:
        args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

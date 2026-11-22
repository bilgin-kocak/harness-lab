"""Project the math study's cost from its smoke test (plan A9), before any further paid run.

Usage: python project_cost.py [study_dir]   (default: this file's directory)

Reads <study_dir>/smoke/attempts.jsonl (written by `harnesslab study search --stage smoke`) and
prints, from measured numbers only:

* per configuration: attempts, mean cost per attempt, mean API calls per attempt, mean cost per
  call, tokens, wall time and verdicts;
* the projected cost of each later step two ways: the plan's call counts (A9 table) times the
  measured cost per call, and an upper bound that prices every non-baseline attempt like the
  smoke test's "all knobs on" configuration;
* the held-out reserve the plan asks for (1.25 x the projected held-out cost) and the total
  against the $150 cap, with the drop order of A9 if it does not fit.

Nothing here calls the API or changes the study.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

CAP_USD = 150.0
# Plan A9: estimated API calls per step (attempts in brackets).
STEPS = [
    ("stage 1: 7 configurations x 30 dev x 1", 540, 210, 30),
    ("stage 2 (conditional): baseline + winner, 2 more repeats", 240, 120, 60),
    ("held-out: baseline + frozen x 40 x 3", 480, 240, 120),
]


def _records(study_dir: Path) -> list[dict]:
    path = study_dir / "smoke" / "attempts.jsonl"
    if not path.exists():
        raise SystemExit(f"no smoke records at {path}: run the smoke stage first")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if not rec.get("harness_error"):
                out.append(rec)
    return out


def _charged(r: dict) -> float:
    if r.get("charged_usd") is not None:
        return float(r["charged_usd"])
    return float(r["attempt"]["usage"]["cost_usd"])


def main() -> None:
    study_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    recs = _records(study_dir)
    by: dict[str, list[dict]] = {}
    for r in recs:
        by.setdefault(r.get("config_label") or r["config_hash"][:12], []).append(r)
    print(f"smoke records: {len(recs)}")
    total_cost = sum(_charged(r) for r in recs)
    total_calls = sum(int(r["attempt"]["usage"].get("llm_calls") or 0) for r in recs)
    per_call = total_cost / total_calls if total_calls else float("nan")
    print(f"smoke cost ${total_cost:.4f}; {total_calls} API calls; ${per_call:.4f} per call\n")
    print(
        f"{'configuration':<12} {'n':>3} {'$/attempt':>10} {'calls':>6} {'$/call':>8} "
        f"{'in tok':>8} {'out tok':>8} {'wall s':>7}  verdicts"
    )
    per_attempt: dict[str, float] = {}
    for label, rs in sorted(by.items()):
        costs = [_charged(r) for r in rs]
        calls = [int(r["attempt"]["usage"].get("llm_calls") or 0) for r in rs]
        cin = [int(r["attempt"]["usage"].get("input_tokens") or 0) for r in rs]
        cout = [int(r["attempt"]["usage"].get("output_tokens") or 0) for r in rs]
        walls = [float(r["attempt"].get("wall_time_s") or 0.0) for r in rs]
        verdicts: dict[str, int] = {}
        for r in rs:
            v = (r.get("verdict") or {}).get("status", "none")
            verdicts[v] = verdicts.get(v, 0) + 1
        per_attempt[label] = statistics.fmean(costs)
        cpc = sum(costs) / sum(calls) if sum(calls) else float("nan")
        print(
            f"{label:<12} {len(rs):>3} {statistics.fmean(costs):>10.4f} "
            f"{statistics.fmean(calls):>6.2f} {cpc:>8.4f} {statistics.fmean(cin):>8.0f} "
            f"{statistics.fmean(cout):>8.0f} {statistics.fmean(walls):>7.1f}  {verdicts}"
        )
    base = per_attempt.get("baseline")
    allon = per_attempt.get("all-on")
    print("\nprojection (plan A9 call counts x measured $/call | upper bound):")
    spent = total_cost
    totals = [spent, spent]
    held_plan = held_upper = 0.0
    for name, calls, attempts, baseline_attempts in STEPS:
        plan = calls * per_call
        upper = (
            baseline_attempts * base + (attempts - baseline_attempts) * allon
            if base is not None and allon is not None
            else float("nan")
        )
        totals[0] += plan
        totals[1] += upper
        if name.startswith("held-out"):
            held_plan, held_upper = plan, upper
        print(f"  {name:<58} ${plan:8.2f} | ${upper:8.2f}")
    print(f"  {'smoke (spent)':<58} ${spent:8.2f} | ${spent:8.2f}")
    print(f"  {'total':<58} ${totals[0]:8.2f} | ${totals[1]:8.2f}   (cap ${CAP_USD:.0f})")
    print(
        f"\nheld-out reserve (1.25 x projected held-out): ${1.25 * held_plan:.2f} (plan counts) | "
        f"${1.25 * held_upper:.2f} (upper bound)"
    )
    no_stage2 = totals[0] - STEPS[1][1] * per_call
    print(f"total without stage 2 (plan counts): ${no_stage2:.2f}")
    if no_stage2 > CAP_USD:
        print(
            "Over the cap even without stage 2: A9 drops stage-1 configurations in this order: "
            "F+context48k, F+retries2, F+planning; lowering effort needs a new smoke test."
        )


if __name__ == "__main__":
    main()

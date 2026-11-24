"""Project the pilot's cost from its smoke tests, before the pilot stage runs (PLAN.md, step 4).

Usage: python studies/2026-11-math-pilot/project_cost.py

Reads ``haiku/smoke/attempts.jsonl`` and ``opus/smoke/attempts.jsonl`` and the math study's
held-out records, calls nothing and changes nothing. It prints:

* the measured smoke cost per attempt, per model and configuration;
* the projected cost of the pilot stage (40 problems x 3 repetitions per configuration).

Projections:

* **Opus R4** is projected from the math study's held-out baseline on the same 40 problems: a
  problem's cost per call c and its baseline success rate s give an expected c * (1 + (1-s) +
  (1-s)^2 + (1-s)^3) for up to four independent tries. The pessimistic case charges four calls
  wherever the baseline ever failed. Each case is also scaled by the smoke test's measured /
  predicted ratio on its 3 problems.
* **Haiku** is projected two ways:
  * the smoke test's mean cost per attempt times 120 attempts per configuration (two of the
    three smoke problems are hard ones, so this leans high);
  * an upper bound that charges every attempt its full call cap at the dearest call the smoke
    test saw.
"""

from __future__ import annotations

import gzip
import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
MATH = HERE.parent / "2026-11-math-minif2f" / "held_out" / "attempts.jsonl.gz"
CAPS = {"haiku": 10.0, "opus": 30.0}
CALL_CAP = {"baseline": 1, "F": 4, "R4": 4}
REPS = 3
OPUS_R4_RESERVATION = 6.41  # adapter worst case of one R4 cell, reserved before it starts


def _smoke(model: str) -> list[dict]:
    path = HERE / model / "smoke" / "attempts.jsonl"
    if not path.is_file():
        raise SystemExit(f"no smoke records at {path}: run the smoke stage first")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if not r.get("harness_error")]


def _cost(r: dict) -> float:
    if r.get("charged_usd") is not None:
        return float(r["charged_usd"])
    return float(r["metrics"]["cost_usd"])


def _table(model: str, recs: list[dict]) -> dict[str, dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by[r["config_label"]].append(r)
    print(f"\n{model}: {len(recs)} smoke attempts, ${sum(_cost(r) for r in recs):.4f}")
    print(
        f"  {'config':<9}{'n':>3}{'$/attempt':>11}{'calls':>7}{'$/call':>9}{'out tok':>9}"
        f"{'cap stops':>10}{'refusals':>9}{'wall s':>8}  verified"
    )
    out = {}
    for label, rs in sorted(by.items()):
        costs = [_cost(r) for r in rs]
        calls = [int(r["metrics"].get("llm_calls") or 0) for r in rs]
        per_call = [c / n for c, n in zip(costs, calls, strict=True) if n]
        print(
            f"  {label:<9}{len(rs):>3}{statistics.fmean(costs):>11.4f}"
            f"{statistics.fmean(calls):>7.2f}"
            f"{(sum(costs) / sum(calls) if sum(calls) else float('nan')):>9.4f}"
            f"{statistics.fmean(int(r['metrics'].get('output_tokens') or 0) for r in rs):>9.0f}"
            f"{sum(int(r['metrics'].get('max_tokens_stops') or 0) for r in rs):>10}"
            f"{sum(int(r['metrics'].get('refusals') or 0) for r in rs):>9}"
            f"{statistics.fmean(float(r['metrics'].get('wall_time_s') or 0) for r in rs):>8.1f}"
            f"  {[r['task_id'] for r in rs if r['metrics'].get('verified')]}"
        )
        out[label] = {
            "mean": statistics.fmean(costs),
            "max_call": max(per_call) if per_call else 0.0,
            "tasks": {r["task_id"]: _cost(r) for r in rs},
        }
    return out


def _math_baseline() -> dict[str, tuple[float, float]]:
    """Per problem: the Opus baseline's cost per call and success rate (math study, held-out)."""
    acc: dict[str, list[tuple[int, float, int]]] = defaultdict(list)
    with gzip.open(MATH, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r["config_label"] == "baseline":
                m = r["metrics"]
                acc[r["task_id"]].append((int(bool(m["verified"])), m["cost_usd"], m["llm_calls"]))
    return {
        t: (sum(x[1] for x in v) / sum(x[2] for x in v), sum(x[0] for x in v) / len(v))
        for t, v in acc.items()
    }


def _r4(c: float, s: float, pessimistic: bool) -> float:
    if pessimistic:
        return c * (4 if s < 1 else 1)
    return c * sum((1 - s) ** k for k in range(4))


def main() -> None:
    base = _math_baseline()
    total_spent, total_projected = 0.0, 0.0

    opus = _table("opus", _smoke("opus"))
    measured = opus["R4"]["tasks"]
    print("\nopus R4 projection for 40 problems x 3 (from the math study's baseline):")
    for pessimistic in (False, True):
        name = "pessimistic" if pessimistic else "expected"
        predicted_smoke = sum(_r4(*base[t], pessimistic) for t in measured)
        ratio = sum(measured.values()) / predicted_smoke
        projected = REPS * sum(_r4(c, s, pessimistic) for c, s in base.values())
        print(
            f"  {name:<12} ${projected:7.2f} as modelled; smoke measured/predicted {ratio:.2f} "
            f"-> ${projected * ratio:7.2f}"
        )
        if pessimistic:
            opus_plan = max(projected, projected * ratio)
    opus_spent = sum(measured.values())
    print(
        f"  planning figure (pessimistic, the larger of the two) ${opus_plan:.2f}; with the smoke "
        f"spend ${opus_spent + opus_plan:.2f} against the ${CAPS['opus']:.0f} cap; the last cell "
        f"needs ${OPUS_R4_RESERVATION:.2f} of headroom: "
        f"{'ok' if opus_spent + opus_plan + OPUS_R4_RESERVATION <= CAPS['opus'] else 'TOO TIGHT'}"
    )
    total_spent += opus_spent
    total_projected += opus_plan

    haiku = _table("haiku", _smoke("haiku"))
    print("\nhaiku projection for 40 problems x 3 per configuration:")
    lean_total, upper_total = 0.0, 0.0
    for label, row in sorted(haiku.items()):
        lean = row["mean"] * 40 * REPS
        upper = 40 * REPS * CALL_CAP[label] * row["max_call"]
        lean_total += lean
        upper_total += upper
        print(f"  {label:<9} ${lean:7.2f} (smoke mean x 120) | upper bound ${upper:7.2f}")
    haiku_spent = sum(sum(r["tasks"].values()) for r in haiku.values())
    print(
        f"  total     ${lean_total:7.2f} | upper bound ${upper_total:7.2f}; with the smoke spend "
        f"${haiku_spent + lean_total:.2f} | ${haiku_spent + upper_total:.2f} against the "
        f"${CAPS['haiku']:.0f} cap"
    )
    total_spent += haiku_spent
    total_projected += lean_total

    print(
        f"\nsmoke spent ${total_spent:.2f}; pilot stage projected ${total_projected:.2f} "
        f"(Opus pessimistic + Haiku smoke-mean); total ${total_spent + total_projected:.2f} "
        f"against caps of ${sum(CAPS.values()):.0f}"
    )


if __name__ == "__main__":
    main()

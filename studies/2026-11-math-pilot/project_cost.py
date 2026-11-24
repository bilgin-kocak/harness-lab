"""Project the pilot's cost from its smoke tests, before the pilot stage runs (PLAN.md, step 4).

Usage: python studies/2026-11-math-pilot/project_cost.py

Reads the smoke records of ``haiku/`` and ``opus/`` and the math study's held-out records. It calls
nothing and changes nothing. It prints:

* the measured smoke cost per attempt, per model and configuration;
* what each study has spent so far, counted as the protocol's budget counts it (every record's
  charge, harness errors included, plus the full reservation of a cell that never wrote a record);
* the projected cost of the pilot stage (40 problems x 3 repetitions per configuration).

Projections:

* **Opus R4** is projected from the math study's held-out baseline on the same 40 problems. A
  problem's cost per call c and its baseline success rate s give an expected cost of
  c * (1 + (1-s) + (1-s)^2 + (1-s)^3) for up to four independent tries. The pessimistic case
  charges four calls wherever the baseline ever failed. Each case is also scaled by the smoke
  test's measured / predicted ratio on its 3 problems.
* **Haiku** is projected three ways:
  * the smoke mean cost per attempt times 120 attempts per configuration (two of the three smoke
    problems are hard ones, so this leans high);
  * every attempt using its full call cap at the dearest single call the smoke test saw;
  * a hard bound: every possible call at its recorded worst case (``worst_case_usd``, the bound the
    prover enforces before each call).
"""

from __future__ import annotations

import gzip
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from harnesslab.study.store import StudyStore  # noqa: E402

MATH = HERE.parent / "2026-11-math-minif2f" / "held_out" / "attempts.jsonl.gz"
CAPS = {"haiku": 20.0, "opus": 50.0}  # the studies' max_cost_usd (safety ceilings)
CALL_CAP = {"baseline": 1, "F": 4, "R4": 4}
REPS = 3
PARALLELISM = 4
OPUS_R4_RESERVATION = 6.40768  # adapter worst case of one Opus R4 cell, reserved before it starts


def _cost(r: dict) -> float:
    if r.get("charged_usd") is not None:
        return float(r["charged_usd"])
    return float(r["metrics"]["cost_usd"])


def _spent(store: StudyStore) -> float:
    """What the budget counts as spent: every record's charge plus unmatched reservations."""
    total = 0.0
    for phase in ("smoke", "dev", "held_out"):
        total += sum(_cost(r) for r in store.records(phase))
        total += sum(float(e["usd"]) for e in store.dangling_reservations(phase))
    return total


def _calls(r: dict) -> list[dict]:
    return ((r.get("attempt") or {}).get("details") or {}).get("calls") or []


def _table(model: str, store: StudyStore) -> dict[str, dict]:
    recs = store.effective("smoke")
    if not recs:
        raise SystemExit(f"no smoke records for {model}: run its smoke stage first")
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by[r["config_label"]].append(r)
    print(f"\n{model}: {len(recs)} valid smoke attempts, ${sum(_cost(r) for r in recs):.4f}")
    print(
        f"  {'config':<9}{'n':>3}{'$/attempt':>11}{'calls':>7}{'$/call':>9}{'out tok':>9}"
        f"{'cap stops':>10}{'refusals':>9}{'wall s':>8}  status / verified"
    )
    out = {}
    for label, rs in sorted(by.items()):
        costs = [_cost(r) for r in rs]
        calls = [int(r["metrics"].get("llm_calls") or 0) for r in rs]
        each = [c for r in rs for c in _calls(r)]
        statuses: dict[str, int] = defaultdict(int)
        for r in rs:
            statuses[str((r.get("attempt") or {}).get("status"))] += 1
        print(
            f"  {label:<9}{len(rs):>3}{statistics.fmean(costs):>11.4f}"
            f"{statistics.fmean(calls):>7.2f}"
            f"{(sum(costs) / sum(calls) if sum(calls) else float('nan')):>9.4f}"
            f"{statistics.fmean(int(r['metrics'].get('output_tokens') or 0) for r in rs):>9.0f}"
            f"{sum(int(r['metrics'].get('max_tokens_stops') or 0) for r in rs):>10}"
            f"{sum(int(r['metrics'].get('refusals') or 0) for r in rs):>9}"
            f"{statistics.fmean(float(r['metrics'].get('wall_time_s') or 0) for r in rs):>8.1f}"
            f"  {dict(statuses)} {[r['task_id'] for r in rs if r['metrics'].get('verified')]}"
        )
        out[label] = {
            "mean": statistics.fmean(costs),
            "max_call": max((float(c.get("cost_usd") or 0) for c in each), default=0.0),
            "worst_call": max((float(c.get("worst_case_usd") or 0) for c in each), default=0.0),
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

    opus_store = StudyStore(HERE / "opus")
    opus = _table("opus", opus_store)
    measured = opus["R4"]["tasks"]
    print("\nopus R4 projection for 40 problems x 3 (from the math study's baseline):")
    opus_plan = 0.0
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
    opus_spent = _spent(opus_store)
    headroom = CAPS["opus"] - opus_spent - opus_plan
    print(
        f"  planning figure (pessimistic, the larger of the two) ${opus_plan:.2f}; spent so far "
        f"${opus_spent:.2f}; ceiling ${CAPS['opus']:.0f}. {PARALLELISM} cells at once to the end "
        f"needs ${PARALLELISM * OPUS_R4_RESERVATION:.2f} of headroom, there is ${headroom:.2f}: "
        f"{'ok' if headroom >= PARALLELISM * OPUS_R4_RESERVATION else 'TOO TIGHT'}"
    )
    total_spent += opus_spent
    total_projected += opus_plan

    haiku_store = StudyStore(HERE / "haiku")
    haiku = _table("haiku", haiku_store)
    print("\nhaiku projection for 40 problems x 3 per configuration:")
    lean_total = upper_total = hard_total = 0.0
    for label, row in sorted(haiku.items()):
        lean = row["mean"] * 40 * REPS
        upper = 40 * REPS * CALL_CAP[label] * row["max_call"]
        hard = 40 * REPS * CALL_CAP[label] * row["worst_call"]
        lean_total, upper_total, hard_total = (
            lean_total + lean,
            upper_total + upper,
            hard_total + hard,
        )
        print(
            f"  {label:<9} ${lean:7.2f} (smoke mean x 120) | every call cap at the dearest call "
            f"${upper:7.2f} | hard bound ${hard:7.2f}"
        )
    haiku_spent = _spent(haiku_store)
    print(
        f"  total     ${lean_total:7.2f} | ${upper_total:7.2f} | ${hard_total:7.2f}; spent so far "
        f"${haiku_spent:.2f}; ceiling ${CAPS['haiku']:.0f}: the hard bound "
        f"{'fits' if haiku_spent + hard_total <= CAPS['haiku'] else 'does NOT fit'}"
    )
    total_spent += haiku_spent
    total_projected += lean_total

    print(
        f"\nsmoke spent ${total_spent:.2f}; pilot stage projected ${total_projected:.2f} "
        f"(Opus pessimistic + Haiku smoke mean); total ${total_spent + total_projected:.2f}. "
        f"Safety ceilings ${CAPS['haiku']:.0f} + ${CAPS['opus']:.0f}."
    )


if __name__ == "__main__":
    main()

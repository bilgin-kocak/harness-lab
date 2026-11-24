"""The pilot's exploratory analysis, as PLAN.md fixes it (written before any pilot data).

Usage: python studies/2026-11-math-pilot/analyze.py [--out results.json]

Reads the pilot stage's records (``haiku/dev``, ``opus/dev``) and the math study's held-out records
(Opus baseline and F, same 40 problems). Calls nothing. For each contrast it prints:

* the per-problem verified rates (verified attempts / repetitions). As in the math study, an
  attempt is verified when the independent verifier accepted its output proof, whatever the
  attempt's status: a capped attempt's last proof is checked too. Refusals and attempts with no
  proof are not verified;
* the mean difference with a 95% percentile bootstrap interval over problems (10,000 resamples,
  seed 20261124), wins, losses and ties, the exact two-sided sign test, and every problem whose
  rates differ (``harnesslab.study.stats.paired_rates``).

It first checks that every configuration has 3 valid records on each of the 40 problems, and says
so if one does not. It also prints the attempts per status, the cost per model and configuration,
every attempt that stopped at the output token limit, the Opus decomposition and the R4 repeat
count.
Everything here is exploratory: 40 problems, used once before, three repetitions.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from harnesslab.study.stats import paired_rates  # noqa: E402
from harnesslab.study.store import StudyStore  # noqa: E402

RESAMPLES = 10_000
REPS = 3
SEED = 20261124
MATH = HERE.parent / "2026-11-math-minif2f"


def _outcomes(records: list[dict]) -> dict[str, dict[str, list[bool]]]:
    """config label -> task -> verified outcomes, one per repetition."""
    out: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        out[r["config_label"]][r["task_id"]].append(bool((r.get("metrics") or {}).get("verified")))
    return out


def _complete(name: str, outcomes: dict[str, dict[str, list[bool]]], labels, tasks) -> bool:
    ok = True
    for label in labels:
        got = outcomes.get(label, {})
        short = {t: len(got.get(t, [])) for t in tasks if len(got.get(t, [])) != REPS}
        if short:
            ok = False
            print(
                f"INCOMPLETE {name} {label}: {len(short)} problems without {REPS} records {short}"
            )
    return ok


def _contrast(name: str, a: dict[str, list[bool]], b: dict[str, list[bool]]) -> dict:
    res = paired_rates(a, b, resamples=RESAMPLES, seed=SEED, level=0.95)
    diff = res.diff
    differ = sorted(
        (t, f"{sum(a[t])}/{len(a[t])}", f"{sum(b[t])}/{len(b[t])}")
        for t in res.tasks
        if abs(sum(b[t]) / len(b[t]) - sum(a[t]) / len(a[t])) > 1e-9
    )
    print(
        f"\n{name}: n={res.n}, rates {res.rate_a:.3f} -> {res.rate_b:.3f}, "
        f"difference {diff.estimate:+.3f} [{diff.low:+.3f}, {diff.high:+.3f}], "
        f"{res.wins}/{res.losses}/{res.ties} (wins/losses/ties), sign test p = "
        f"{res.sign_test_p if res.sign_test_p is not None else 'n/a'}"
    )
    for t, ra, rb in differ:
        print(f"    {t:<48} {ra} -> {rb}")
    return {"contrast": name, **res.model_dump(mode="json"), "differing": differ}


def _cost(label: str, model: str, records: list[dict]) -> dict:
    n = len(records)
    cost = sum(float(r.get("charged_usd") or r["metrics"]["cost_usd"]) for r in records)
    verified = sum(bool(r["metrics"].get("verified")) for r in records)
    m = [r["metrics"] for r in records]
    row = {
        "model": model,
        "config": label,
        "attempts": n,
        "verified": verified,
        "cost_usd": round(cost, 4),
        "usd_per_attempt": round(cost / n, 5) if n else None,
        "usd_per_verified": round(cost / verified, 5) if verified else None,
        "calls_per_attempt": round(sum(x.get("llm_calls") or 0 for x in m) / n, 3) if n else None,
        "output_tokens": sum(x.get("output_tokens") or 0 for x in m),
        "max_tokens_stops": sum(x.get("max_tokens_stops") or 0 for x in m),
        "refusals": sum(x.get("refusals") or 0 for x in m),
        "wall_time_s": round(sum(x.get("wall_time_s") or 0 for x in m), 1),
    }
    print(
        f"  {model:<6}{label:<9} {n:>4} attempts, {verified:>4} verified, ${cost:8.4f}, "
        f"${row['usd_per_attempt']}/attempt, ${row['usd_per_verified']}/proof, "
        f"{row['calls_per_attempt']} calls, {row['max_tokens_stops']} cap stops, "
        f"{row['refusals']} refusals"
    )
    return row


def _repeats(records: list[dict]) -> dict:
    """R4 attempts in which a later try returned, word for word, a proof an earlier try returned."""
    attempts = repeated = 0
    for r in records:
        rounds = ((r.get("attempt") or {}).get("details") or {}).get("rounds") or []
        proofs = [x.get("proof_sha256") for x in rounds if x.get("proof_sha256")]
        if len(proofs) > 1:
            attempts += 1
            repeated += len(proofs) != len(set(proofs))
    return {"attempts_with_two_or_more_proofs": attempts, "with_a_repeated_proof": repeated}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    math = StudyStore(MATH).effective("held_out")
    haiku = StudyStore(HERE / "haiku").effective("dev")
    opus = StudyStore(HERE / "opus").effective("dev")
    mo, ho, oo = _outcomes(math), _outcomes(haiku), _outcomes(opus)

    print("EXPLORATORY PILOT: 40 problems used once before, 3 repetitions; not a result to quote.")
    tasks = sorted(mo["baseline"])
    complete = (
        _complete("math study", mo, ("baseline", "F"), tasks)
        & _complete("opus", oo, ("R4",), tasks)
        & _complete("haiku", ho, ("baseline", "F", "R4"), tasks)
    )
    print(f"{len(tasks)} problems; records complete: {complete}")
    results: dict = {"complete": complete, "contrasts": [], "cost": [], "r4_repeats": {}}
    c = results["contrasts"]
    c.append(_contrast("Opus: R4 - baseline (math study)", mo["baseline"], oo["R4"]))
    c.append(_contrast("Opus: F (math study) - R4", oo["R4"], mo["F"]))
    c.append(_contrast("Opus: F - baseline (math study, for reference)", mo["baseline"], mo["F"]))
    c.append(_contrast("Haiku: F - baseline", ho["baseline"], ho["F"]))
    c.append(_contrast("Haiku: R4 - baseline", ho["baseline"], ho["R4"]))
    c.append(_contrast("Haiku: F - R4", ho["R4"], ho["F"]))

    f_b, r4_b, f_r4 = (x["diff"]["estimate"] for x in (c[2], c[0], c[1]))
    print(
        f"\nOpus decomposition of F - baseline ({f_b:+.3f}): R4 - baseline (extra samples, in-loop "
        f"selection) {r4_b:+.3f}, F - R4 (showing the try's history) {f_r4:+.3f}. Rough: two run "
        f"dates{'' if complete else '; records INCOMPLETE, so the parts need not add up'}."
    )
    results["opus_decomposition"] = {"f_minus_b": f_b, "r4_minus_b": r4_b, "f_minus_r4": f_r4}

    print("\ncost:")
    for label in ("baseline", "F"):
        rs = [r for r in math if r["config_label"] == label]
        results["cost"].append(_cost(label, "opus", rs) | {"source": "math study held-out"})
    results["cost"].append(_cost("R4", "opus", opus) | {"source": "pilot"})
    for label in ("baseline", "F", "R4"):
        rs = [r for r in haiku if r["config_label"] == label]
        results["cost"].append(_cost(label, "haiku", rs) | {"source": "pilot"})

    print("\nattempts per status, and attempts that stopped at the output token limit:")
    results["status"], results["max_tokens_attempts"] = {}, []
    for model, recs in (("opus-math", math), ("opus", opus), ("haiku", haiku)):
        for r in recs:
            key = f"{model}/{r['config_label']}/{(r.get('attempt') or {}).get('status')}"
            results["status"][key] = results["status"].get(key, 0) + 1
            stops = int((r.get("metrics") or {}).get("max_tokens_stops") or 0)
            if stops:
                row = [model, r["config_label"], r["task_id"], int(r["repetition"]), stops]
                results["max_tokens_attempts"].append(row)
                print(f"  token cap: {row}")
    for key, n in sorted(results["status"].items()):
        print(f"  {key}: {n}")

    for model, recs in (("opus", opus), ("haiku", haiku)):
        rep = _repeats([r for r in recs if r["config_label"] == "R4"])
        results["r4_repeats"][model] = rep
        print(f"\n{model} R4 repeats: {rep}")

    if args.out:
        args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Project the held-out phase's cost and time from the smoke and setup records (no API call).

Usage: python studies/2026-11-math-feedback-haiku/project_cost.py

Prints, per arm, the measured cost per attempt, calls per attempt and wall time, then projects the
held-out phase (4 arms x 204 problems x 5 repetitions = 4,080 cells):

* **Measured:** each arm's mean cost per attempt times 1,020 attempts. The 30 setup problems are
  the math study's dev problems, a different sample from the held-out set.
* **Pilot-based:** the pilot's measured Haiku cost per attempt on 40 problems from the same test
  split. P is priced as F.
* **Upper bound:** every attempt uses its full call cap at the dearest single call measured.

It then sets each figure against the held-out reserve and the study's cap, and estimates the run
time from the mean wall time per cell at the spec's parallelism.
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from harnesslab.study.spec import load_split, load_study_spec  # noqa: E402
from harnesslab.study.store import StudyStore  # noqa: E402

CALL_CAP = {"baseline": 1, "F": 4, "R4": 4, "P": 4}
PILOT_PER_ATTEMPT = {"baseline": 0.001966, "F": 0.003675, "R4": 0.004183, "P": 0.003675}


def _cost(r: dict) -> float:
    return float(r["charged_usd"] if r.get("charged_usd") is not None else r["metrics"]["cost_usd"])


def main() -> None:
    spec = load_study_spec(HERE / "study.yaml")
    store = StudyStore(HERE)
    n_cells = len(load_split(spec).held_out) * spec.held_out.repetitions  # attempts per arm
    recs = store.effective("smoke") + store.effective("dev")
    if not recs:
        raise SystemExit("no smoke or setup records yet: run the smoke stage first")
    by: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by[r["config_label"]].append(r)
    spent = sum(_cost(r) for ph in ("smoke", "dev", "held_out") for r in store.records(ph))
    spent += sum(float(e["usd"]) for ph in ("smoke", "dev", "held_out")
                 for e in store.dangling_reservations(ph))  # fmt: skip
    print(f"records: {len(recs)} (smoke + setup); spent so far ${spent:.4f}")
    print(
        f"{'arm':<9}{'n':>4}{'$/attempt':>11}{'calls':>7}{'dearest call':>13}{'wall s':>8}  verified"
    )
    measured = pilot = upper = 0.0
    walls = []
    for arm in ("baseline", "F", "R4", "P"):
        rs = by.get(arm, [])
        if not rs:
            print(f"{arm:<9} no records")
            continue
        costs = [_cost(r) for r in rs]
        calls = [int(r["metrics"].get("llm_calls") or 0) for r in rs]
        dearest = max(
            (float(c.get("cost_usd") or 0) for r in rs
             for c in ((r.get("attempt") or {}).get("details") or {}).get("calls") or []),
            default=0.0,
        )  # fmt: skip
        wall = statistics.fmean(float(r.get("cell_seconds") or r["metrics"].get("wall_time_s") or 0)
                                for r in rs)  # fmt: skip
        walls.append(wall)
        print(
            f"{arm:<9}{len(rs):>4}{statistics.fmean(costs):>11.5f}{statistics.fmean(calls):>7.2f}"
            f"{dearest:>13.5f}{wall:>8.1f}  {sum(bool(r['metrics'].get('verified')) for r in rs)}"
        )
        measured += statistics.fmean(costs) * n_cells
        pilot += PILOT_PER_ATTEMPT[arm] * n_cells
        upper += CALL_CAP[arm] * dearest * n_cells
    reserve, cap = spec.budget.held_out_reserve_usd, spec.budget.max_cost_usd
    print(f"\nheld-out projection (4,080 cells): measured ${measured:.2f} | pilot-based ${pilot:.2f} "
          f"| upper bound ${upper:.2f}")  # fmt: skip
    print(f"held-out reserve ${reserve:.2f}: the larger projection "
          f"{'fits' if max(measured, pilot) <= reserve else 'DOES NOT FIT'}; cap ${cap:.2f}, "
          f"spent so far ${spent:.2f}")  # fmt: skip
    hours = (
        4 * n_cells * statistics.fmean(walls) / spec.parallelism / 3600 if walls else float("nan")
    )
    print(
        f"time: about {hours:.1f} h at parallelism {spec.parallelism} (mean cell wall time; "
        "Lean contention can change it)"
    )


if __name__ == "__main__":
    main()

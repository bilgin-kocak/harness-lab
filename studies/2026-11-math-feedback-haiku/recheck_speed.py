"""Was the machine of the held-out run faster than the one `recheck_heavy_cpu.py` measured on?

EXPLORATORY (NOT REGISTERED). Written after the held-out phase, 2026-11-26, after the 450 s
measurement (`recheck_heavy_cpu_450.json`) and before this script was run. That measurement ran in
a new container after the session's machine restarted, and the run's own CPU was not recorded. Its
conclusion (F's proofs of `mathd_numbertheory_451` would have timed out under the registered 300 s
even with enough memory) fails if the run delivered at least 1.5 times as much computation per
second to one Lean check as this container does to a check running alone.

This script measures that ratio on proofs whose checks finished in the run. It re-times, on the
current container and one at a time, the final checks of the run that meet all of these (fixed
before any was re-timed):

* the recorded final check finished (pass or fail, not timed out) in 20 s or more of Lean time,
  so that loading the imports is a small part of it;
* the problem is not `mathd_numbertheory_451`, and the check is not one `d1_report.json` assigns
  to a kernel kill or matches by time only;
* the check, taken as the interval of its recorded Lean time ending at the record's
  `finished_at`, does not overlap a check of `mathd_numbertheory_451` (`heavy_windows` in
  `d1_report.json`), so the run's timing is not slowed by that problem's memory pressure.

For each, ratio = recorded Lean time in the run / Lean time now. A ratio below 1 means the run was
faster on that proof. Reported: the number of checks, the median ratio, the 10th and 90th
percentiles, the share below 1, and the smallest ratio; and the verdicts, which should repeat.
The run checked with 3 Lean workers at once (`lean_workers: 3`, `parallelism: 4`), so its times
include that load; this re-timing runs one check at a time with nothing else running, so it shows
the run's speed under its own load against this container's speed alone, which is the comparison
the 300 s question needs. No model is called.

Usage (repository root, pinned toolchain on PATH)::

    python studies/2026-11-math-feedback-haiku/recheck_speed.py [--progress FILE]

With ``--progress FILE``, each verdict is appended to FILE as it is made, and a rerun resumes.
Writes ``recheck_speed.json`` in the study directory.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEAVY = "mathd_numbertheory_451"
MIN_LEAN_S = 20.0


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _recheck_module():
    spec = importlib.util.spec_from_file_location("recheck", HERE / "recheck.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lean_s(r: dict) -> float | None:
    return ((((r.get("verdict") or {}).get("details") or {}).get("durations")) or {}).get("lean_s")


def _timed_out(r: dict) -> bool:
    return bool(((((r.get("verdict") or {}).get("details") or {}).get("checks")) or {}).get("timed_out"))


def select(records: list[dict], rc) -> list[dict]:
    d1 = json.loads((HERE / "d1_report.json").read_text(encoding="utf-8"))
    windows = [(_t(a), _t(b)) for _, a, b in d1["heavy_windows"]]
    killed = rc._killed_keys()
    out = []
    for r in records:
        ls = _lean_s(r)
        if ls is None or ls < MIN_LEAN_S or _timed_out(r):
            continue
        if r["task_id"] == HEAVY or rc._key(r) in killed:
            continue
        end = _t(r["finished_at"])
        start = end - timedelta(seconds=ls)
        if any(start <= b and a <= end for a, b in windows):
            continue
        out.append(r)
    return sorted(out, key=rc._key)


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    i = q * (len(xs) - 1)
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--progress", type=Path)
    args = ap.parse_args()
    rc = _recheck_module()
    records = rc._records()
    selected = select(records, rc)
    by_key = {rc._key(r): r for r in selected}
    print(f"EXPLORATORY (NOT REGISTERED) speed comparison: {len(selected)} checks", flush=True)
    rows = asyncio.run(rc.run(selected, 1, args.progress))
    out_rows = []
    for x in rows:
        r = by_key[(x["arm"], x["task_id"], x["repetition"])]
        then, now = _lean_s(r), x["recheck"]["lean_s"]
        out_rows.append(
            {
                **x,
                "run_lean_s": then,
                "now_lean_s": now,
                "ratio_run_over_now": round(then / now, 4) if now else None,
            }
        )
    ratios = [x["ratio_run_over_now"] for x in out_rows if x["ratio_run_over_now"] is not None]
    try:
        cpu = next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        )
    except (OSError, StopIteration):
        cpu = None
    summary = {
        "label": "exploratory (not registered); written after the held-out phase ended",
        "method": __doc__.split("Usage")[0].strip(),
        "cpu_now": cpu,
        "checks": len(out_rows),
        "same_status": sum(1 for x in out_rows if x["same_status"]),
        "ratio_median": round(statistics.median(ratios), 3),
        "ratio_p10": round(_pct(ratios, 0.10), 3),
        "ratio_p90": round(_pct(ratios, 0.90), 3),
        "ratio_min": round(min(ratios), 3),
        "share_below_1": round(sum(1 for v in ratios if v < 1) / len(ratios), 3),
        "rows": out_rows,
    }
    (HERE / "recheck_speed.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("rows", "method")}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

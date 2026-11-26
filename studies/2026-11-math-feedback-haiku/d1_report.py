"""The reports DEVIATIONS.md D1 promised, and the exploratory analyses it named.

EXPLORATORY (NOT REGISTERED). Written after the held-out phase ended (2026-11-26), with the
records available; nothing here changes or replaces the registered results, which are
``analyze.py``'s output. ``analyze.py`` is imported for its helpers and left unchanged.

Usage (from the repository root)::

    python studies/2026-11-math-feedback-haiku/d1_report.py [--study-dir DIR]

Writes ``d1_report.json`` in the study directory; ``d1_report.md`` is the written account of it,
together with the Lean re-checks in ``recheck_killed.json``.

Inputs: the held-out records and reservations, and the kernel's out-of-memory reports kept in
``held_out_snapshot/kernel_oom_boot_<boot time>.txt``. Kernel times are the boot time in the file
name plus the kernel's seconds; record times are truncated to the second, so a record's window is
taken as [started_at, finished_at + 1 s].

Matching rules (D1, "What is reported with the results"):

* A **kill** is a kernel "Killed process <pid> (lean|lake)" line. One under 1 GB anon-rss is a
  **bystander** (listed separately). The kernel's "kB" are KiB.
* **Final check**: a kill inside the cell's window and within 2 s of its ``finished_at``, whatever
  the verdict's reason or exit code.
* **In-loop check**: a kill inside the cell's window, and a round whose in-loop reason is "lean
  exited with code 137" or "... -9".
* A kill matching no check is **unmatched**, listed with the cells in progress.
* **Exposed** cell: one that started while another cell of ``mathd_numbertheory_451`` was running
  (from that cell's ``started_at`` to its ``finished_at``; for the interrupted cell, from its
  reservation's ``started_at`` to the kill of the held-out process).

Added after the phase (D1 did not name it): **one-to-one assignment**. The time rule can match a
kill to several checks, and a check to several kills, when two checks end within seconds. Each
kill kills one process, so each is assigned to one check:

* a kill with one candidate goes to it;
* a kill of 1 GB or more goes to the candidate check of ``mathd_numbertheory_451`` (every large
  kill with a single candidate was one, at 13.2-13.9 GB);
* a bystander goes to the candidate that is not a check of ``mathd_numbertheory_451`` and that
  ended with code 137 (a final check's exit code, or an in-loop reason);
* a check left without a kill is "matched by time only".
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from harnesslab.study.store import StudyStore  # noqa: E402

_spec = importlib.util.spec_from_file_location("registered_analyze", HERE / "analyze.py")
A = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(A)  # definitions only; analyze.main() is not run

ARMS = A.ARMS
HEAVY = "mathd_numbertheory_451"
BYSTANDER_BYTES = 10**9
FINAL_TOL_S = 2.0
KILLED_INLOOP = ("lean exited with code 137", "lean exited with code -9")
KILL_RE = re.compile(
    r"^\[\s*([0-9.]+)\]\s.*Killed process (\d+) \((\w+)\).*?anon-rss:(\d+)kB"
)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def kernel_kills(study_dir: Path) -> list[dict]:
    files = sorted((study_dir / "held_out_snapshot").glob("kernel_oom_boot_*.txt"))
    out = []
    for f in files:
        boot = datetime.strptime(
            f.stem.removeprefix("kernel_oom_boot_"), "%Y-%m-%dT%H-%M-%S"
        ).replace(tzinfo=timezone.utc)
        for line in f.read_text(encoding="utf-8").splitlines():
            m = KILL_RE.match(line)
            if not m:
                continue
            secs, pid, comm, kib = float(m[1]), int(m[2]), m[3], int(m[4])
            out.append(
                {
                    "time": boot + timedelta(seconds=secs),
                    "kernel_s": secs,
                    "pid": pid,
                    "process": comm,
                    "anon_rss_bytes": kib * 1024,
                    "bystander": comm in ("lean", "lake") and kib * 1024 < BYSTANDER_BYTES,
                    "boot_file": f.name,
                }
            )
    return sorted(out, key=lambda k: k["time"])


class Run:
    def __init__(self, study_dir: Path) -> None:
        self.d = A.Data(study_dir)
        self.store = StudyStore(study_dir)
        self.kills = kernel_kills(study_dir)
        self.records = self.d.records
        self.arm = {id(r): self.d.labels[r["config_hash"]] for r in self.records}
        self.window = {
            id(r): (_t(r["started_at"]), _t(r["finished_at"]) + timedelta(seconds=1))
            for r in self.records
        }
        ids = {r.get("reservation_id") for r in self.d.all_records}
        res = [
            json.loads(x)
            for x in _read_lines(study_dir / "held_out" / "reservations.jsonl")
            if x.strip()
        ]
        self.interrupted = [x for x in res if x["id"] not in ids]
        self.runner_kills = [k for k in self.kills if k["process"] == "python"]

    # ------------------------------------------------------------------ matching

    def in_window(self, r: dict, t: datetime) -> bool:
        a, b = self.window[id(r)]
        return a <= t <= b

    def final_matches(self) -> list[dict]:
        out = []
        for k in self.kills:
            if k["process"] not in ("lean", "lake"):
                continue
            for r in self.records:
                fin = _t(r["finished_at"])
                # finished_at is truncated to the second: the true end is in [fin, fin + 1 s),
                # so "within 2 s of the end" is -2 s <= kill - fin <= +3 s
                gap = (k["time"] - fin).total_seconds()
                if self.in_window(r, k["time"]) and -FINAL_TOL_S <= gap <= FINAL_TOL_S + 1:
                    out.append(self._row(k, r, "final"))
        return out

    def inloop_matches(self) -> list[dict]:
        out = []
        for r in self.records:
            rounds = A._details(r).get("rounds") or []
            killed = [
                i + 1
                for i, rd in enumerate(rounds)
                if (rd.get("inloop") or {}).get("reason") in KILLED_INLOOP
            ]
            if not killed:
                continue
            ks = [
                k
                for k in self.kills
                if k["process"] in ("lean", "lake") and self.in_window(r, k["time"])
            ]
            for k in ks or [None]:
                row = self._row(k, r, "in-loop")
                row["rounds"] = killed
                out.append(row)
        return out

    def _row(self, k: dict | None, r: dict, kind: str) -> dict:
        c = A._verdict_checks(r)
        return {
            "kind": kind,
            "kill_time": _iso(k["time"]) if k else None,
            "kill_pid": k["pid"] if k else None,
            "kill_anon_rss_gb": round(k["anon_rss_bytes"] / 1e9, 3) if k else None,
            "bystander": k["bystander"] if k else None,
            "arm": self.arm[id(r)],
            "task_id": r["task_id"],
            "repetition": int(r["repetition"]),
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "verified": A._verified(r),
            "verdict_reason": (r.get("verdict") or {}).get("reason"),
            "exit_code": c.get("exit_code"),
            "timed_out": c.get("timed_out"),
            "lean_s": A._verifier_seconds(r),
        }

    def assign(self, raw: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """One check per kill (see the module docstring). Returns (assigned, time_only,
        ambiguous kills)."""
        by_kill: dict[tuple, list[dict]] = defaultdict(list)
        for m in raw:
            by_kill[(m["kill_time"], m["kill_pid"])].append(m)
        assigned, ambiguous, taken = [], [], set()

        def check_key(m: dict) -> tuple:
            return (m["kind"], m["arm"], m["task_id"], m["repetition"])

        for k in self.kills:
            cands = [m for m in by_kill.get((_iso(k["time"]), k["pid"]), [])]
            if not cands:
                continue
            if len(cands) == 1:
                pick, how = cands[0], "only candidate"
            elif not k["bystander"]:
                pool = [m for m in cands if m["task_id"] == HEAVY]
                pick = pool[0] if len(pool) == 1 else None
                how = f"{k['anon_rss_bytes'] / 1e9:.2f} GB: the candidate check of {HEAVY}"
            else:
                pool = [
                    m
                    for m in cands
                    if m["task_id"] != HEAVY
                    and (m["kind"] == "in-loop" or m["exit_code"] == 137)
                ]
                pick = pool[0] if len(pool) == 1 else None
                how = "under 1 GB: the candidate not of " + HEAVY + " that ended with code 137"
            if pick is None or check_key(pick) in taken:
                ambiguous.append({"kill_time": _iso(k["time"]), "kill_pid": k["pid"],
                                  "candidates": [check_key(m) for m in cands]})
                continue
            taken.add(check_key(pick))
            assigned.append({**pick, "assigned_by": how})
        time_only = []
        seen = set()
        for m in raw:
            key = check_key(m)
            if key not in taken and key not in seen:
                seen.add(key)
                time_only.append({**m, "kill_time": None, "kill_pid": None,
                                  "kill_anon_rss_gb": None, "bystander": None})
        return assigned, time_only, ambiguous

    def unmatched(self, matched: list[dict]) -> list[dict]:
        used = {(m["kill_time"], m["kill_pid"]) for m in matched if m["kill_time"]}
        out = []
        for k in self.kills:
            key = (_iso(k["time"]), k["pid"])
            if key in used:
                continue
            running = [
                f"{self.arm[id(r)]} {r['task_id']} rep {r['repetition']}"
                for r in self.records
                if self.in_window(r, k["time"])
            ]
            interrupted = [
                f"{self.d.labels.get(x['config_hash'], '?')} {x['task_id']} rep {x['repetition']} "
                "(interrupted, no record)"
                for x in self.interrupted
                if self.runner_kills
                and _t(x["started_at"])
                <= k["time"]
                <= self.runner_kills[-1]["time"] + timedelta(seconds=1)  # the same event
            ]
            out.append(
                {
                    "kill_time": key[0],
                    "kill_pid": k["pid"],
                    "process": k["process"],
                    "anon_rss_gb": round(k["anon_rss_bytes"] / 1e9, 3),
                    "bystander": k["bystander"],
                    "cells_in_progress": sorted(running) + interrupted,
                }
            )
        return out

    # ------------------------------------------------------------------ exposure

    def heavy_windows(self) -> list[tuple[str, datetime, datetime]]:
        w = [
            (f"record {r['finished_at']}", _t(r["started_at"]), _t(r["finished_at"]))
            for r in self.records
            if r["task_id"] == HEAVY
        ]
        end = self.runner_kills[-1]["time"] if self.runner_kills else None
        for x in self.interrupted:
            if x["task_id"] == HEAVY and end is not None:
                w.append((f"interrupted, reserved {x['started_at']}", _t(x["started_at"]), end))
        return w

    def exposed(self, strict: bool = False) -> set[int]:
        """Cells that started while another cell of the heavy problem ran. Times are to the
        second: the inclusive rule counts a start in the same second as that cell's start or end;
        the strict rule does not."""
        w = self.heavy_windows()
        out = set()
        for r in self.records:
            s = _t(r["started_at"])
            for _, a, b in w:
                inside = (a < s < b) if strict else (a <= s <= b)
                if inside and not (r["task_id"] == HEAVY and a == s):
                    out.add(id(r))
                    break
        return out

    def exposure_table(self, exposed: set[int]) -> dict:
        def row(rs: list[dict]) -> dict:
            rounds = [rd for r in rs for rd in A._details(r).get("rounds") or []]
            waits = [
                float(
                    (((r.get("verdict") or {}).get("details") or {}).get("durations") or {}).get(
                        "wait_s"
                    )
                    or 0
                )
                for r in rs
            ]
            return {
                "cells": len(rs),
                "inloop_timeouts": sum(
                    1 for rd in rounds if (rd.get("inloop") or {}).get("reason") == "timeout"
                ),
                "verifier_timeouts": sum(1 for r in rs if A._verdict_checks(r).get("timed_out")),
                "time_cap_stops": sum(
                    1 for r in rs if str(A._details(r).get("stop") or "") == "cap:time"
                ),
                "final_check_waits_over_60s": sum(1 for w in waits if w > 60),
                "max_final_check_wait_s": round(max(waits), 1) if waits else None,
                "verified_share_exploratory": (
                    statistics.fmean(A._verified(r) for r in rs) if rs else None
                ),
            }

        out = {}
        for arm in ARMS:
            rs = [r for r in self.records if self.arm[id(r)] == arm]
            out[arm] = {
                "exposed": row([r for r in rs if id(r) in exposed]),
                "unexposed": row([r for r in rs if id(r) not in exposed]),
            }
        return out

    # ------------------------------------------------------------------ exploratory contrasts

    def contrasts_without(self, drop_tasks: set[str], label: str) -> dict:
        rates = self.d.rates()
        tasks = [t for t in self.d.usable if t not in drop_tasks]
        h1 = A._contrast(f"H1 F - R4, {label}", rates, "R4", "F", tasks, "exploratory")
        h2 = A._contrast(f"H2 F - P, {label}", rates, "P", "F", tasks, "exploratory")
        h3 = A._contrast(f"H3 P - R4, {label}", rates, "R4", "P", tasks, "exploratory")
        adj = A.holm({"H2": h2["sign_flip_p"], "H3": h3["sign_flip_p"]})
        h1["reading_if_registered_rule"] = A.three_way(h1["sign_flip_p"], h1["ci90"], h1["estimate"])
        h2["holm_p"], h3["holm_p"] = adj["H2"], adj["H3"]
        h2["reading_if_registered_rule"] = A.three_way(adj["H2"], h2["ci95"], h2["estimate"])
        h3["reading_if_registered_rule"] = A.three_way(adj["H3"], h3["ci95"], h3["estimate"])
        return {"H1": h1, "H2": h2, "H3": h3}

    def h1_without_pairs(self, pairs: set[tuple[str, int]], label: str) -> dict:
        keep = {
            t: [k for k in self.d.common[t] if (t, k) not in pairs] for t in self.d.usable
        }
        tasks = [t for t in self.d.usable if keep[t]]
        rates = {
            a: {
                t: statistics.fmean(A._verified(self.d.cell[(a, t, k)]) for k in keep[t])
                for t in tasks
            }
            for a in ARMS
        }
        out = A._contrast(f"H1 F - R4, {label}", rates, "R4", "F", tasks, "exploratory")
        out["reading_if_registered_rule"] = A.three_way(
            out["sign_flip_p"], out["ci90"], out["estimate"]
        )
        out["pairs_dropped"] = len(pairs)
        out["problems_dropped_entirely"] = sorted(set(self.d.usable) - set(tasks))
        return out


def counterfactuals(run: "Run", assigned: list[dict], time_only: list[dict]) -> dict:
    """H1-H3 if kill-ended (and late-timeout) final checks had passed instead: how far the
    registered readings depend on the cells the memory problem touched. Added after the phase:
    D1 bounded the mean differences but did not consider the p-values."""
    import contextlib
    import io

    d = run.d
    base = d.rates()
    killed = {(m["arm"], m["task_id"], m["repetition"]) for m in assigned if m["kind"] == "final"}
    late = {(m["arm"], m["task_id"], m["repetition"]) for m in time_only if m["kind"] == "final"}
    killed = {c for c in killed if not A._verified(d.cell[c])}
    late = {c for c in late if not A._verified(d.cell[c])}

    def rates_with(flip: set) -> dict:
        out = {a: dict(base[a]) for a in base}
        for a, t, _ in flip:
            out[a][t] = statistics.fmean(
                True if (a, t, k) in flip else A._verified(d.cell[(a, t, k)]) for k in d.common[t]
            )
        return out

    cases = {
        "F's kill-ended cells pass": {c for c in killed if c[0] == "F"},
        "F's kill-ended and late-timeout cells pass": {c for c in killed | late if c[0] == "F"},
        "R4's kill-ended cells pass": {c for c in killed if c[0] == "R4"},
        "P's kill-ended cells pass": {c for c in killed if c[0] == "P"},
        "every kill-ended cell passes": killed,
        "every kill-ended and late-timeout cell passes": killed | late,
    }
    scenarios = {}
    for label, flip in cases.items():
        r = rates_with(flip)
        with contextlib.redirect_stdout(io.StringIO()):
            hs = {
                "H1": A._contrast("H1", r, "R4", "F", d.usable, "exploratory"),
                "H2": A._contrast("H2", r, "P", "F", d.usable, "exploratory"),
                "H3": A._contrast("H3", r, "R4", "P", d.usable, "exploratory"),
            }
        adj = A.holm({"H2": hs["H2"]["sign_flip_p"], "H3": hs["H3"]["sign_flip_p"]})
        row = {"cells_flipped": sorted([list(c) for c in flip])}
        for name, h in hs.items():
            p_used = h["sign_flip_p"] if name == "H1" else adj[name]
            row[name] = {
                "estimate": h["estimate"],
                "ci95": h["ci95"],
                "ci90": h["ci90"],
                "wins_losses_ties": h["wins_losses_ties"],
                "sign_flip_p": h["sign_flip_p"],
                "holm_p": None if name == "H1" else adj[name],
                "reading_if_registered_rule": A.three_way(
                    p_used, h["ci90"] if name == "H1" else h["ci95"], h["estimate"]
                ),
            }
        scenarios[label] = row

    # Added after the re-checks: the cells whose re-check passes, and every subset of F's
    # killed cells of the heavy problem, with and without F's late-timeout cell.
    import itertools

    other = {c for c in killed | late if c[1] != HEAVY}
    f_heavy = sorted(c for c in killed if c[0] == "F" and c[1] == HEAVY)
    f_late = {c for c in late if c[0] == "F"}

    def h1_h2(flip: set) -> tuple[dict, dict]:
        r = rates_with(flip)
        with contextlib.redirect_stdout(io.StringIO()):
            h1 = A._contrast("H1", r, "R4", "F", d.usable, "exploratory")
            h2 = A._contrast("H2", r, "P", "F", d.usable, "exploratory")
            h3 = A._contrast("H3", r, "R4", "P", d.usable, "exploratory")
        adj = A.holm({"H2": h2["sign_flip_p"], "H3": h3["sign_flip_p"]})
        return h1, {**h2, "holm_p": adj["H2"]}

    h1, h2 = h1_h2(other)
    scenarios["the cells on other problems (aime_1990_p15 F, imo_2019_p1 B) pass"] = {
        "cells_flipped": sorted([list(c) for c in other]),
        "H1": {"estimate": h1["estimate"], "ci90": h1["ci90"],
               "wins_losses_ties": h1["wins_losses_ties"], "sign_flip_p": h1["sign_flip_p"],
               "reading_if_registered_rule": A.three_way(h1["sign_flip_p"], h1["ci90"], h1["estimate"])},
        "H2": {"estimate": h2["estimate"], "ci95": h2["ci95"], "holm_p": h2["holm_p"],
               "reading_if_registered_rule": A.three_way(h2["holm_p"], h2["ci95"], h2["estimate"])},
    }
    subsets = []
    for with_late in (False, True):
        for k in range(1, len(f_heavy) + 1):
            ps = []
            for combo in itertools.combinations(f_heavy, k):
                h1, h2 = h1_h2(set(combo) | (f_late if with_late else set()))
                ps.append((h1["sign_flip_p"], h1["estimate"], h2["holm_p"]))
            subsets.append({
                "f_heavy_cells_passing": k,
                "with_f_late_timeout_cell": with_late,
                "subsets": len(ps),
                "h1_p_min": min(x[0] for x in ps),
                "h1_p_max": max(x[0] for x in ps),
                "h1_estimate": ps[0][1],
                "h2_holm_p_min": min(x[2] for x in ps),
                "h2_holm_p_max": max(x[2] for x in ps),
            })
    scenarios["subsets of F's killed cells of the heavy problem"] = subsets
    return scenarios


def _read_lines(path: Path) -> list[str]:
    import gzip

    gz = path.with_name(path.name + ".gz")
    if path.exists():
        return path.read_text(encoding="utf-8").splitlines()
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        return f.read().splitlines()


def bounds(cells_in: list[dict], n_problems: int, reps: int) -> dict:
    """The most a verdict change on the given unverified final checks could move each mean
    difference: each cell is 1/reps of its problem's rate, so 1/(reps * n) of the mean."""
    per_cell = 1 / (reps * n_problems)
    cells = {
        (m["arm"], m["task_id"], m["repetition"])
        for m in cells_in
        if m["kind"] == "final" and not m["verified"]
    }
    by_arm = Counter(a for a, _, _ in cells)
    out = {
        "per_cell_points": round(100 * per_cell, 4),
        "unverified_cells_by_arm": dict(sorted(by_arm.items())),
        "cells": sorted([list(c) for c in cells]),
    }
    contrasts = (
        ("H1 F - R4", "F", "R4"), ("H2 F - P", "F", "P"), ("H3 P - R4", "P", "R4"),
        ("R4 - baseline", "R4", "baseline"), ("F - baseline", "F", "baseline"),
        ("P - baseline", "P", "baseline"),
    )
    for name, b, a in contrasts:
        out[name] = {
            "could_rise_by_points": round(100 * by_arm.get(b, 0) * per_cell, 3),
            "could_fall_by_points": round(100 * by_arm.get(a, 0) * per_cell, 3),
        }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--study-dir", type=Path, default=HERE)
    args = p.parse_args()
    run = Run(args.study_dir)
    print("EXPLORATORY (NOT REGISTERED): DEVIATIONS.md D1 reports, written after the phase ended")

    final = run.final_matches()
    inloop = run.inloop_matches()
    unmatched = run.unmatched(final + inloop)
    assigned, time_only, ambiguous = run.assign(final + inloop)
    killed_table: dict[str, Counter] = defaultdict(Counter)
    for m in assigned:
        killed_table[f"{m['kind']} check"][f"{m['arm']} {m['task_id']}"] += 1
    exposed = run.exposed()
    exposed_pairs = {
        (r["task_id"], int(r["repetition"])) for r in run.records if id(r) in exposed
    }
    matched_pairs = {(m["task_id"], m["repetition"]) for m in final + inloop}  # time rule
    out = {
        "label": "exploratory (not registered); DEVIATIONS.md D1; written after the phase ended",
        "kernel_kills": [
            {**k, "time": _iso(k["time"]), "anon_rss_gb": round(k["anon_rss_bytes"] / 1e9, 3)}
            for k in run.kills
        ],
        "final_check_matches_time_rule": final,
        "inloop_check_matches_time_rule": inloop,
        "assigned_one_to_one": assigned,
        "matched_by_time_only": time_only,
        "ambiguous_kills": ambiguous,
        "killed_checks_by_arm_and_problem": {k: dict(sorted(v.items())) for k, v in killed_table.items()},
        "d1_literal_count": {
            "final_checks_matched_by_the_time_rule": len(
                {(m["arm"], m["task_id"], m["repetition"]) for m in final}
            ),
            "inloop_checks_matched": len(
                {(m["arm"], m["task_id"], m["repetition"]) for m in inloop}
            ),
        },
        "unmatched_kills": unmatched,
        "interrupted_cells": [
            {
                "arm": run.d.labels.get(x["config_hash"], "?"),
                "task_id": x["task_id"],
                "repetition": x["repetition"],
                "reserved_at": x["started_at"],
                "charged_usd": x["usd"],
                "kill": (
                    f"held-out process killed by the kernel at {_iso(run.runner_kills[-1]['time'])}"
                    if run.runner_kills
                    else None
                ),
            }
            for x in run.interrupted
        ],
        "heavy_windows": [[n, _iso(a), _iso(b)] for n, a, b in run.heavy_windows()],
        "exposure": run.exposure_table(exposed),
        "exposure_strict_boundaries": run.exposure_table(run.exposed(strict=True)),
        "exposed_pairs": len(exposed_pairs),
        "exposed_cells": len(exposed),
        "exposed_cells_strict": len(run.exposed(strict=True)),
        "exploratory": {
            "without_" + HEAVY: run.contrasts_without({HEAVY}, f"without {HEAVY}"),
            "h1_without_exposed_pairs": run.h1_without_pairs(
                exposed_pairs, "without (problem, repetition) pairs with an exposed cell"
            ),
            "h1_without_exposed_pairs_strict": run.h1_without_pairs(
                {
                    (r["task_id"], int(r["repetition"]))
                    for r in run.records
                    if id(r) in run.exposed(strict=True)
                },
                "without (problem, repetition) pairs with an exposed cell, strict boundaries",
            ),
            "h1_without_kill_matched_pairs": run.h1_without_pairs(
                matched_pairs,
                "without (problem, repetition) pairs with a kernel-matched check "
                "(added after the phase: D1 did not name it)",
            ),
        },
        "counterfactual_readings": counterfactuals(run, assigned, time_only),
        "bounds_assigned_kills": bounds(assigned, len(run.d.usable), run.d.reps),
        "bounds_assigned_kills_and_time_only": bounds(
            assigned + time_only, len(run.d.usable), run.d.reps
        ),
    }
    target = args.study_dir / "d1_report.json"
    target.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

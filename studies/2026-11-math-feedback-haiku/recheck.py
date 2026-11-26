"""Re-check recorded held-out proofs with the Lean kernel, after the phase ended.

EXPLORATORY (NOT REGISTERED). Written after the held-out phase ended (2026-11-26). It calls no
model: it runs the adapter's verifier (the registered one, with its 300 s timeout) on the proofs
the records hold, and compares each new verdict with the recorded one. Lean is deterministic, so
a verdict should repeat unless the original check was disturbed (a kill, a timeout under load).

Usage (from the repository root, with the pinned toolchain on PATH)::

    python studies/2026-11-math-feedback-haiku/recheck.py SET [--workers N] [--out FILE]

Sets:

* ``differing``: every cell (all four arms, all repetitions) of each problem whose rates differ
  in H1, H2 or H3 (``analysis.json``), except ``mathd_numbertheory_451`` (see ``killed``).
* ``sample``: 60 further cells per arm, drawn with seed 20261126 from the cells not in
  ``differing`` or ``killed``.
* ``killed``: every final check that ``d1_report.json`` assigns to a kernel kill or matches by
  time only. Run with ``--workers 1`` and nothing else running: a check of
  ``mathd_numbertheory_451`` reached 13.2-13.9 GB of the session's 14.3 GB memory limit.

Writes ``recheck_<SET>.json`` in the study directory (or ``--out``).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

HEAVY = "mathd_numbertheory_451"


def _records() -> list[dict]:
    with gzip.open(HERE / "held_out" / "attempts.jsonl.gz", "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip() and not json.loads(x).get("harness_error")]


def _key(r: dict) -> tuple[str, str, int]:
    return (r["config_label"], r["task_id"], int(r["repetition"]))


def _differing_problems() -> set[str]:
    a = json.loads((HERE / "analysis.json").read_text(encoding="utf-8"))
    out = {t for t, *_ in a["primary"]["differing"]}
    for v in a["secondary"].values():
        out |= {t for t, *_ in v["differing"]}
    return out - {HEAVY}


def _killed_keys() -> set[tuple[str, str, int]]:
    d = json.loads((HERE / "d1_report.json").read_text(encoding="utf-8"))
    rows = d["assigned_one_to_one"] + d["matched_by_time_only"]
    return {(m["arm"], m["task_id"], int(m["repetition"])) for m in rows if m["kind"] == "final"}


def select(name: str, records: list[dict]) -> list[dict]:
    killed = _killed_keys()
    differing = _differing_problems()
    if name == "killed":
        return [r for r in records if _key(r) in killed]
    if name == "differing":
        return [r for r in records if r["task_id"] in differing and _key(r) not in killed]
    if name == "sample":
        rng = random.Random(20261126)
        out = []
        for arm in ("baseline", "F", "R4", "P"):
            pool = sorted(
                (
                    r
                    for r in records
                    if r["config_label"] == arm
                    and r["task_id"] not in differing
                    and r["task_id"] != HEAVY
                    and _key(r) not in killed
                ),
                key=_key,
            )
            out += rng.sample(pool, 60)
        return out
    raise SystemExit(f"unknown set {name!r}")


async def run(selected: list[dict], workers: int) -> list[dict]:
    from harnesslab.domains import ensure_builtin_domains
    from harnesslab.study.contract import Attempt, VerifyContext
    from harnesslab.study.registry import create_domain
    from harnesslab.study.spec import load_study_spec

    spec = load_study_spec(HERE / "study.yaml")
    ensure_builtin_domains()
    adapter = create_domain(spec.adapter, spec.adapter_options, spec.study_dir)
    tasks = {t.id: t for t in adapter.load_tasks(sorted({r["task_id"] for r in selected}))}
    gate = asyncio.Semaphore(workers)
    out: list[dict] = []

    async def one(r: dict, tmp: Path) -> None:
        async with gate:
            t0 = time.monotonic()
            v = await adapter.verify(
                tasks[r["task_id"]],
                Attempt.model_validate(r["attempt"]),
                VerifyContext(
                    study=spec.name, phase="held_out", attempt_dir=tmp, keep_artifacts=False
                ),
            )
            new = v.model_dump(mode="json")
            old = r.get("verdict") or {}
            oc = ((old.get("details") or {}).get("checks")) or {}
            nc = ((new.get("details") or {}).get("checks")) or {}
            row = {
                "arm": r["config_label"],
                "task_id": r["task_id"],
                "repetition": int(r["repetition"]),
                "proof_sha256": (r["attempt"].get("output") or {}).get("proof_sha256"),
                "recorded": {
                    "status": old.get("status"),
                    "reason": old.get("reason"),
                    "exit_code": oc.get("exit_code"),
                    "timed_out": oc.get("timed_out"),
                },
                "recheck": {
                    "status": new.get("status"),
                    "reason": new.get("reason"),
                    "exit_code": nc.get("exit_code"),
                    "timed_out": nc.get("timed_out"),
                    "lean_s": ((new.get("details") or {}).get("durations") or {}).get("lean_s"),
                    "wall_s": round(time.monotonic() - t0, 3),
                },
            }
            row["same_status"] = row["recorded"]["status"] == row["recheck"]["status"]
            out.append(row)
            print(
                f"{len(out)}/{len(selected)} {row['arm']} {row['task_id']} rep {row['repetition']}: "
                f"{row['recorded']['status']} -> {row['recheck']['status']}"
                f" ({row['recheck']['reason'][:60] if row['recheck']['reason'] else ''})",
                flush=True,
            )

    try:
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.gather(*(one(r, Path(tmp)) for r in selected))
    finally:
        close = getattr(adapter, "close", None)
        if close is not None:
            await close()
    return sorted(out, key=lambda x: (x["task_id"], x["arm"], x["repetition"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=("differing", "sample", "killed"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    selected = select(args.set, _records())
    print(f"EXPLORATORY (NOT REGISTERED) re-check, set {args.set}: {len(selected)} cells", flush=True)
    rows = asyncio.run(run(selected, args.workers))
    summary = {
        "label": "exploratory (not registered); written after the held-out phase ended",
        "set": args.set,
        "cells": len(rows),
        "same_status": sum(1 for x in rows if x["same_status"]),
        "changed": [x for x in rows if not x["same_status"]],
        "rows": rows,
    }
    target = args.out or HERE / f"recheck_{args.set}.json"
    target.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"{summary['same_status']}/{len(rows)} verdicts repeat; wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

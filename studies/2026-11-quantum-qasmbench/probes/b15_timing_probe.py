"""Plan amendment B15.10: a timing probe on DEV circuits only. NOT a study result.

Cell time (compile + verify) of 8 stage-1 configurations drawn with random.Random(7) from the
720-configuration grid, on the 22 dev circuits stage 1 uses. It ran on 2026-11-23 with verifier
v2, before the cross-check skip of B15.9, on a machine shared with other jobs (load about 6 on 4
CPUs), so its times overstate the search's. Only the per-cell seconds informed the amendment;
no ranking, selection or claim uses its verdicts. Raw output: b15_timing_probe_cells.jsonl.

Usage: HARNESSLAB_QASMBENCH_DIR=<QASMBench at 357b942> python b15_timing_probe.py 7 8
(run from a scratch directory: it writes cells.jsonl and tmp/ there).
"""

import asyncio
import itertools
import json
import random
import sys
import time
from pathlib import Path

WT = str(Path(__file__).resolve().parents[3])
sys.path.insert(0, WT + "/src")
from harnesslab.domains.quantum.adapter import QiskitQasmBenchAdapter  # noqa: E402
from harnesslab.domains.quantum.compile import BASELINE  # noqa: E402
from harnesslab.study.contract import Config, RunContext, VerifyContext  # noqa: E402

STUDY = Path(WT) / "studies/2026-11-quantum-qasmbench"
split = json.loads((STUDY / "split.json").read_text())
dev = split["assignment"]["A"]["dev"] + split["assignment"]["B"]["dev"]
skip = {
    "medium/vqe_n24",
    "medium/qft_n18",
    "medium/knn_n25",
}  # unverifiable at the baseline (dropped by tasks_from)
grid = list(
    itertools.product(
        [0, 1, 2, 3],
        ["default", "dense", "sabre", "trivial"],
        ["default", "basic", "sabre"],
        ["default", "synthesis", "translator"],
        [0, 1, 2, 3, 4],
    )
)
rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 7)
sample = rng.sample(grid, int(sys.argv[2]) if len(sys.argv) > 2 else 8)


async def main():
    adapter = QiskitQasmBenchAdapter(
        {"compile_timeout_s": 1200, "workers": 3, "compile_repeats": 3}, STUDY
    )
    tasks = [t for t in adapter.load_tasks(dev) if t.id not in skip]
    sem = asyncio.Semaphore(3)
    out = open("cells.jsonl", "a")

    async def cell(task, values, i):
        async with sem:
            cfg = Config(
                values={
                    **BASELINE,
                    "optimization_level": values[0],
                    "layout_method": values[1],
                    "routing_method": values[2],
                    "translation_method": values[3],
                    "seed_transpiler": values[4],
                }
            )
            d = Path("tmp") / f"{i}"
            t0 = time.perf_counter()
            a = await adapter.run(
                task,
                cfg,
                RunContext(
                    study="probe",
                    phase="dev",
                    repetition=0,
                    attempt_dir=d,
                    keep_artifacts=False,
                    cost_limit_usd=None,
                    seed=0,
                ),
            )
            t1 = time.perf_counter()
            v = await adapter.verify(
                task,
                a,
                VerifyContext(study="probe", phase="dev", attempt_dir=d, keep_artifacts=False),
            )
            t2 = time.perf_counter()
            rec = {
                "task": task.id,
                "pool": task.data["pool"],
                "config": values,
                "status": a.status.value,
                "verdict": v.status.value,
                "by": v.details.get("decided_by"),
                "cached": v.details.get("cached"),
                "run_s": round(t1 - t0, 3),
                "verify_s": round(t2 - t1, 3),
                "cell_s": round(t2 - t0, 3),
                "active": v.details.get("active_qubits"),
                "two_q": (v.details.get("counts") or {}).get("two_qubit_gates"),
            }
            out.write(json.dumps(rec) + "\n")
            out.flush()

    try:
        jobs = [
            cell(t, values, i)
            for i, (values, t) in enumerate((v, t) for v in sample for t in tasks)
        ]
        await asyncio.gather(*jobs)
    finally:
        await adapter.close()


asyncio.run(main())

"""Checks behind plan amendment B14, on dev circuits only (run before any search cell).

Usage: python -I amendment_checks.py <QASMBench checkout at 357b942> <split.json>

1. Which dev circuits contain MCX-like gates (recursively, through custom gate definitions).
2. Whether hls_config(mcx=...) changes any baseline output (all dev circuits but vqe_n24).
3. Whether the adapter's explicit baseline arguments give the same outputs as the plain call
   generate_preset_pass_manager(target=TARGET, seed_transpiler=0).
4. Lookahead routing at level 2 on three dev circuits, with a 30 s limit each.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap, Target
from qiskit.transpiler.passes import HLSConfig
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

STANDARD = {
    "cx", "ccx", "u", "u1", "u2", "u3", "rz", "sx", "x", "h", "cz", "swap", "cswap", "cu1", "cu3",
    "rzz", "crz", "cry", "crx", "ch", "cy", "p", "cp", "s", "sdg", "t", "tdg", "ry", "rx", "y",
    "z", "id", "barrier", "measure", "reset",
}  # fmt: skip


def target() -> Target:
    cm = CouplingMap.from_heavy_hex(5, bidirectional=True)
    return Target.from_configuration(basis_gates=["cz", "rz", "sx", "x"], coupling_map=cm)


def load(repo: Path, cid: str) -> QuantumCircuit:
    size, name = cid.split("/")
    qc = QuantumCircuit.from_qasm_file(str(repo / size / name / f"{name}.qasm"))
    qc.remove_final_measurements()
    return qc


def op_names(circ: QuantumCircuit, seen: set[str]) -> set[str]:
    for inst in circ.data:
        op = inst.operation
        seen.add(op.name)
        definition = getattr(op, "definition", None)
        if definition is not None and op.name not in STANDARD and len(definition.data) < 200_000:
            op_names(definition, seen)
    return seen


def signature(c: QuantumCircuit) -> tuple:
    return (
        dict(c.count_ops()),
        c.depth(),
        float(c.global_phase),
        [
            (
                i.operation.name,
                tuple(c.find_bit(q).index for q in i.qubits),
                tuple(float(p) for p in i.operation.params),
            )
            for i in c.data
        ],
    )


def lookahead(args: tuple[str, str]) -> float:
    repo, cid = args
    pm = generate_preset_pass_manager(
        optimization_level=2, target=target(), seed_transpiler=0, routing_method="lookahead"
    )
    t0 = time.time()
    pm.run(load(Path(repo), cid))
    return time.time() - t0


def main() -> None:
    repo, split = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
    pools = split["assignment"]
    dev = [d["id"] if isinstance(d, dict) else d for d in pools["A"]["dev"] + pools["B"]["dev"]]
    mcx = {}
    for cid in dev:
        names = op_names(load(repo, cid), set())
        hits = sorted(n for n in names if "mcx" in n or n in ("c3x", "c4x", "c3sqrtx", "rcccx"))
        if hits:
            mcx[cid] = hits
    print("1. MCX-like gates in dev circuits:", mcx or "none")
    t = target()
    explicit_diff, hls_diff = [], []
    for cid in dev:
        if cid == "medium/vqe_n24":
            continue
        qc = load(repo, cid)
        base = generate_preset_pass_manager(target=t, seed_transpiler=0).run(qc)
        explicit = generate_preset_pass_manager(
            optimization_level=2,
            target=t,
            seed_transpiler=0,
            approximation_degree=1.0,
            qubits_initially_zero=True,
        ).run(qc)
        if signature(base) != signature(explicit):
            explicit_diff.append(cid)
        for plugin in ("noaux_v24", "gray_code"):
            alt = generate_preset_pass_manager(
                optimization_level=2,
                target=t,
                seed_transpiler=0,
                hls_config=HLSConfig(mcx=[plugin]),
            ).run(qc)
            if signature(alt) != signature(base):
                hls_diff.append((cid, plugin))
    print("2. hls_mcx changes the output on:", hls_diff or "none")
    print("3. explicit baseline arguments differ from the plain call on:", explicit_diff or "none")
    for cid in ("medium/bv_n19", "medium/qec9xz_n17", "small/sat_n7"):
        with mp.Pool(1) as pool:
            res = pool.apply_async(lookahead, ((str(repo), cid),))
            try:
                print(f"4. lookahead {cid}: {res.get(timeout=30):.1f} s")
            except mp.TimeoutError:
                print(f"4. lookahead {cid}: did not finish within 30 s")
                pool.terminate()


if __name__ == "__main__":
    main()

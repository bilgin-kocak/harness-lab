"""Freeze the dev and held-out circuit lists of the QASMBench (Qiskit) study.

Reads a checkout of pnnl/QASMBench at the pinned commit, classifies every circuit with the pinned
Qiskit, and writes ``split.json``: the fixed compilation target, every circuit with its status
(and, for skipped circuits, the reason), and the dev / held-out assignment.

Eligibility uses the input circuit only, never a compilation result:

1. ``<size>/<name>/<name>.qasm`` exists and parses with ``QuantumCircuit.from_qasm_file``;
2. after ``remove_final_measurements`` it has no measurement, reset or classical control left;
3. it has at least one multi-qubit gate (otherwise there is nothing to compile);
4. pool ``A``: at most 10 qubits (``small/``), checked by the exact operator check;
   pool ``B``: 11 to 27 qubits (``medium/``), used only if the decision-diagram checker is
   approved (see the experiment plan); ``large/`` is out of reach of any check used here.

Assignment is per pool: circuits are ranked by ``sha256(f"{seed}:{circuit_id}")`` and alternate
held-out, dev, held-out, ... down the ranking, so pool B can be added later without moving any
pool-A circuit.

Usage (with the pinned Qiskit)::

    git clone https://github.com/pnnl/QASMBench /tmp/QASMBench
    git -C /tmp/QASMBench checkout 357b942396d5c2b7cbc1c229c585a6ef5ccaebac
    python studies/2026-11-quantum-qasmbench/select_tasks.py /tmp/QASMBench [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/pnnl/QASMBench"
PINNED_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
QISKIT_VERSION = "2.6.2"
SEED = 20261122
POOL_A_MAX_QUBITS = 10
POOL_B_MAX_QUBITS = 27
# One target for every circuit: distance 5 gives 57 physical qubits, enough for pool B.
TARGET_DISTANCE = 5
NON_UNITARY = {"measure", "reset", "if_else", "while_loop", "for_loop", "switch_case", "box"}
OUT = Path(__file__).with_name("split.json")


def rank_key(circuit_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{circuit_id}".encode()).hexdigest()


def classify(path: Path, size: str) -> dict:
    from qiskit import QuantumCircuit  # pinned: QISKIT_VERSION

    row: dict = {
        "id": f"{size}/{path.parent.name}",
        "file": f"{size}/{path.parent.name}/{path.name}",
    }
    if not path.exists():
        return row | {"status": "skipped", "reason": "no <name>.qasm file in the circuit folder"}
    row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if size == "large":
        return row | {
            "status": "skipped",
            "reason": "large/: too large for any equivalence check used here",
        }
    try:
        circ = QuantumCircuit.from_qasm_file(str(path))
    except Exception as exc:  # noqa: BLE001 - every parser failure is a recorded skip reason
        first = str(exc).splitlines()[0][:160]
        return row | {
            "status": "skipped",
            "reason": f"does not parse: {type(exc).__name__}: {first}",
        }
    stripped = circ.remove_final_measurements(inplace=False)
    ops = sorted({inst.operation.name for inst in stripped.data})
    row |= {"qubits": stripped.num_qubits, "ops": ops}
    bad = sorted(set(ops) & NON_UNITARY)
    if bad:
        return row | {
            "status": "skipped",
            "reason": f"not unitary after removing final measurements: {', '.join(bad)}",
        }
    multi = sum(
        1
        for inst in stripped.data
        if inst.operation.num_qubits >= 2 and inst.operation.name != "barrier"
    )
    row["multi_qubit_gates"] = multi
    if multi == 0:
        return row | {"status": "skipped", "reason": "no multi-qubit gate: nothing to compile"}
    if stripped.num_qubits <= POOL_A_MAX_QUBITS:
        return row | {"status": "eligible", "pool": "A"}
    if stripped.num_qubits <= POOL_B_MAX_QUBITS:
        return row | {"status": "eligible", "pool": "B"}
    return row | {"status": "skipped", "reason": f"more than {POOL_B_MAX_QUBITS} qubits"}


def target_spec() -> dict:
    from qiskit.transpiler import CouplingMap

    cmap = CouplingMap.from_heavy_hex(TARGET_DISTANCE, bidirectional=True)
    return {
        "description": f"heavy-hex lattice, code distance {TARGET_DISTANCE}, both directions on every edge",
        "constructor": f"qiskit.transpiler.CouplingMap.from_heavy_hex({TARGET_DISTANCE}, bidirectional=True)",
        "num_qubits": cmap.size(),
        "edges": sorted([list(e) for e in cmap.get_edges()]),
        "basis_gates": ["cz", "rz", "sx", "x"],
        "target": "Target.from_configuration(basis_gates=basis_gates, coupling_map=<edges>)",
    }


def build(repo: Path) -> dict:
    import qiskit

    if qiskit.__version__ != QISKIT_VERSION:
        raise SystemExit(f"qiskit {qiskit.__version__} found, the study pins {QISKIT_VERSION}")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != PINNED_COMMIT:
        raise SystemExit(f"{repo} is at {head}, expected the pinned commit {PINNED_COMMIT}")
    rows = []
    for size in ("small", "medium", "large"):
        for folder in sorted(p for p in (repo / size).iterdir() if p.is_dir()):
            rows.append(classify(folder / f"{folder.name}.qasm", size))
    assignment: dict[str, dict[str, list[str]]] = {}
    for pool in ("A", "B"):
        ids = sorted((r["id"] for r in rows if r.get("pool") == pool), key=rank_key)
        assignment[pool] = {"held_out": ids[0::2], "dev": ids[1::2]}
    for r in rows:
        if "pool" in r:
            r["role"] = "held_out" if r["id"] in assignment[r["pool"]]["held_out"] else "dev"
    dev_all = assignment["A"]["dev"] + assignment["B"]["dev"]
    held_all = assignment["A"]["held_out"] + assignment["B"]["held_out"]
    if set(dev_all) & set(held_all):
        raise SystemExit("dev and held-out overlap")
    return {
        "study": "2026-11-quantum-qasmbench",
        "dataset": {
            "repo": REPO_URL,
            "commit": PINNED_COMMIT,
            "license": "Battelle Memorial Institute BSD-style license with PNNL/DOE notice (LICENSE, NOTICE)",
        },
        "qiskit": QISKIT_VERSION,
        "target": target_spec(),
        "selection": {
            "seed": SEED,
            "method": "per pool: rank by sha256(f'{seed}:{id}'), alternate held-out, dev, held-out, ...",
            "pool_A": f"eligible, at most {POOL_A_MAX_QUBITS} qubits: exact operator check",
            "pool_B": f"eligible, {POOL_A_MAX_QUBITS + 1}-{POOL_B_MAX_QUBITS} qubits: only with the decision-diagram checker",
        },
        "assignment": assignment,
        "circuits": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path, help="checkout of QASMBench at the pinned commit")
    parser.add_argument("--check", action="store_true", help="fail if split.json would change")
    args = parser.parse_args()
    text = json.dumps(build(args.repo.resolve()), indent=2) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            sys.exit(f"{OUT} does not match a fresh selection")
        print(f"{OUT.name} reproduced")
        return
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

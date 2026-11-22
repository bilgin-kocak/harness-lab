"""Independent checks of a compiled circuit: target validity and the exact isometry check.

Equivalence (plan section B8): the input is the circuit after ``remove_final_measurements``;
the output is the compiled circuit with its layout. They are equivalent when, for every input
state of the circuit's qubits and with every other physical qubit starting in |0>, the compiled
circuit produces the input circuit's output, up to one global phase, on the physical qubits its
final layout names, with every other qubit back in |0>.

Target validity comes first: every operation must be a basis gate of the target (barriers and
delays are ignored) and every two-qubit gate must act on an edge of the coupling map. An output
that cannot run on the target fails, whatever it computes.

The exact check (pool A) simulates the compiled circuit on its active physical qubits only: the
qubits an operation touches plus the initial and final positions of every input qubit. Idle
ancillas stay in |0> and are left out. All 2^n basis inputs go through at once as a
(2^k x 2^n) array, with the four basis-gate matrices written out below (not taken from Qiskit),
consecutive single-qubit gates fused per qubit, and the circuit's global phase applied. The
reference is ``qiskit.quantum_info.Operator`` of the input circuit (little-endian). The
expected output E is each reference column placed on the final positions with every other
active qubit in |0>. Pass iff |<E, Psi>| / 2^n >= 1 - 1e-9 and, after removing the global phase,
every amplitude is within 1e-7 of E. More than ``max_active`` active qubits: unverifiable.
Memory is 16 * 2^(k+n) bytes: 256 MiB at the limits k = 14, n = 10.
"""

from __future__ import annotations

import math
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from harnesslab.domains.quantum.compile import (
    DIRECTIVES,
    LayoutProblem,
    cached_input,
    layout_positions,
    load_compiled,
)

EXACT_CHECK_ID = "exact-isometry/1"
TARGET_CHECK_ID = "target-validity/1"
FIDELITY_TOLERANCE = 1e-9
AMPLITUDE_TOLERANCE = 1e-7

_X = np.array([[0, 1], [1, 0]], dtype=np.complex128)
_SX = 0.5 * np.array([[1 + 1j, 1 - 1j], [1 - 1j, 1 + 1j]], dtype=np.complex128)


def rz_matrix(theta: float) -> np.ndarray:
    half = 0.5 * theta
    return np.array([[np.exp(-1j * half), 0], [0, np.exp(1j * half)]], dtype=np.complex128)


def gate_matrix(name: str, params: Sequence[Any]) -> np.ndarray:
    """The 2 x 2 matrix of a single-qubit basis gate (row = output, column = input)."""
    if name == "x":
        return _X
    if name == "sx":
        return _SX
    if name == "rz":
        return rz_matrix(float(params[0]))
    raise ValueError(f"the exact check has no matrix for {name!r}")


# --- target validity ------------------------------------------------------------------------


def _finite_angle(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _counts(counter: Counter[Any], limit: int = 5) -> str:
    items = counter.most_common()
    shown = [
        f"{'-'.join(map(str, key)) if isinstance(key, tuple) else key} x{count}"
        for key, count in items[:limit]
    ]
    return ", ".join(shown) + (", ..." if len(items) > limit else "")


def scan_target(circuit: Any, spec: Mapping[str, Any]) -> tuple[list[str], set[int]]:
    """Why ``circuit`` cannot run on the target (empty when it can), and the qubits it touches.

    One pass over the circuit, so pool-B outputs with millions of gates are read once.
    """
    from qiskit.circuit.library import CZGate, RZGate, SXGate, XGate

    standard = {
        "cz": (CZGate, 2, 0),
        "rz": (RZGate, 1, 1),
        "sx": (SXGate, 1, 0),
        "x": (XGate, 1, 0),
    }
    basis = sorted(spec["basis_gates"])
    allowed = {name: standard[name] for name in basis if name in standard}
    edges = {(int(a), int(b)) for a, b in spec["edges"]}
    size = int(spec["num_qubits"])
    index = {bit: i for i, bit in enumerate(circuit.qubits)}
    outside: Counter[str] = Counter()
    impostors: Counter[str] = Counter()
    off_edge: Counter[tuple[int, ...]] = Counter()
    bad_angles = 0
    touched: set[int] = set()
    for inst in circuit.data:
        op = inst.operation
        name = op.name
        if name in DIRECTIVES:
            continue
        qubits = tuple(index[q] for q in inst.qubits)
        touched.update(qubits)
        expected = allowed.get(name)
        if expected is None or inst.clbits:
            outside[name] += 1
            continue
        cls, arity, num_params = expected
        if op.base_class is not cls or len(qubits) != arity or len(op.params) != num_params:
            impostors[name] += 1
            continue
        if arity == 2 and qubits not in edges:
            off_edge[qubits] += 1
        if num_params and not _finite_angle(op.params[0]):
            bad_angles += 1
    problems = []
    if circuit.num_qubits > size:
        problems.append(f"the circuit has {circuit.num_qubits} qubits, the target {size}")
    if outside:
        problems.append(
            f"{outside.total()} operations outside the basis {{{', '.join(basis)}}}: "
            f"{_counts(outside)}"
        )
    if impostors:
        problems.append(
            f"{impostors.total()} operations named like a basis gate but not that gate: "
            f"{_counts(impostors)}"
        )
    if off_edge:
        problems.append(
            f"{off_edge.total()} two-qubit gates on pairs that are not edges of the coupling "
            f"map: {_counts(off_edge)}"
        )
    if bad_angles:
        problems.append(f"{bad_angles} rz gates without a finite numeric angle")
    return problems, touched


# --- exact isometry check -------------------------------------------------------------------


def _operations(circuit: Any) -> list[tuple[str, tuple[int, ...], tuple[Any, ...]]]:
    index = {bit: i for i, bit in enumerate(circuit.qubits)}
    return [
        (inst.operation.name, tuple(index[q] for q in inst.qubits), tuple(inst.operation.params))
        for inst in circuit.data
        if inst.operation.name not in DIRECTIVES
    ]


def _apply_single(state: np.ndarray, k: int, qubit: int, matrix: np.ndarray) -> None:
    """Apply a 2 x 2 matrix to bit ``qubit`` of the row index of ``state``, in place."""
    view = state.reshape(1 << (k - 1 - qubit), 2, -1)
    low = view[:, 0, :]
    high = view[:, 1, :]
    a, b, c, d = matrix[0, 0], matrix[0, 1], matrix[1, 0], matrix[1, 1]
    if b == 0 and c == 0:
        if a != 1:
            low *= a
        if d != 1:
            high *= d
        return
    saved = low.copy()
    low *= a
    low += b * high
    high *= d
    high += c * saved


def _apply_cz(state: np.ndarray, k: int, first: int, second: int) -> None:
    low, high = sorted((first, second))
    view = state.reshape(1 << (k - 1 - high), 2, 1 << (high - low - 1), 2, -1)
    view[:, 1, :, 1, :] *= -1


def _evolve(
    state: np.ndarray,
    k: int,
    operations: Sequence[tuple[str, tuple[int, ...], tuple[Any, ...]]],
    position: Mapping[int, int],
) -> None:
    pending: dict[int, np.ndarray] = {}
    for name, qubits, params in operations:
        if len(qubits) == 1:
            q = position[qubits[0]]
            matrix = gate_matrix(name, params)
            previous = pending.get(q)
            pending[q] = matrix if previous is None else matrix @ previous
        elif name == "cz" and len(qubits) == 2:
            a, b = position[qubits[0]], position[qubits[1]]
            for q in (a, b):
                fused = pending.pop(q, None)
                if fused is not None:
                    _apply_single(state, k, q, fused)
            _apply_cz(state, k, a, b)
        else:
            raise ValueError(f"the exact check cannot apply {name} on {len(qubits)} qubits")
    for q, fused in pending.items():
        _apply_single(state, k, q, fused)


def _max_abs(array: np.ndarray, chunk: int = 1 << 22) -> float:
    flat = array.reshape(-1)
    best = 0.0
    for start in range(0, flat.size, chunk):
        best = max(best, float(np.abs(flat[start : start + chunk]).max(initial=0.0)))
    return best


def exact_isometry_check(
    reference: np.ndarray | Callable[[], np.ndarray],
    circuit: Any,
    initial: Sequence[int],
    final: Sequence[int],
    max_active: int,
) -> dict[str, Any]:
    """Compare ``circuit`` (on its active qubits) with the input's unitary ``reference``.

    ``initial`` and ``final`` are the physical positions of the input qubits. ``reference``
    may be a callable, evaluated only when the active qubits are within ``max_active``.
    Returns ``status`` (``pass``, ``fail`` or ``unverifiable``), ``reason``, the fidelity
    |<E, Psi>| / 2^n, the largest amplitude deviation after removing the global phase, that
    phase, the number of active qubits and the duration.
    """
    started = time.perf_counter()
    n = len(initial)
    operations = _operations(circuit)
    touched = {q for _, qubits, _ in operations for q in qubits}
    active = sorted(touched | set(initial) | set(final))
    k = len(active)
    result: dict[str, Any] = {"active_qubits": k, "max_active_qubits": max_active}
    if k > max_active:
        return result | {
            "status": "unverifiable",
            "reason": f"exact check: {k} active qubits, more than the limit of {max_active}",
            "duration_s": time.perf_counter() - started,
        }
    dim = 1 << n
    unitary = np.asarray(reference() if callable(reference) else reference, dtype=np.complex128)
    if unitary.shape != (dim, dim):
        raise ValueError(f"reference has shape {unitary.shape}, expected {(dim, dim)}")
    position = {p: i for i, p in enumerate(active)}
    columns = np.arange(dim, dtype=np.int64)
    rows_in = np.zeros(dim, dtype=np.int64)
    rows_out = np.zeros(dim, dtype=np.int64)
    for v in range(n):
        bit = (columns >> v) & 1
        rows_in |= bit << position[initial[v]]
        rows_out |= bit << position[final[v]]
    state = np.zeros((1 << k, dim), dtype=np.complex128)
    state[rows_in, columns] = 1.0
    _evolve(state, k, operations, position)
    phase = float(circuit.global_phase)
    if phase:
        state *= np.exp(1j * phase)
    landed = state[rows_out, :]
    overlap = complex(np.vdot(unitary, landed))
    fidelity = abs(overlap) / dim
    rotation = overlap / abs(overlap) if abs(overlap) > 0 else 1.0 + 0.0j
    deviation = float(np.abs(landed - rotation * unitary).max())
    state[rows_out, :] = 0
    max_deviation = max(deviation, _max_abs(state))
    passed = fidelity >= 1 - FIDELITY_TOLERANCE and max_deviation <= AMPLITUDE_TOLERANCE
    verdict = "equivalent" if passed else "not equivalent"
    return result | {
        "status": "pass" if passed else "fail",
        "reason": (
            f"exact check: {verdict} on {k} active qubits (fidelity {fidelity:.12f}, "
            f"largest deviation {max_deviation:.2e})"
        ),
        "fidelity": fidelity,
        "max_deviation": max_deviation,
        "phase": float(np.angle(rotation)),
        "duration_s": time.perf_counter() - started,
    }


# --- the job run in a worker process --------------------------------------------------------

_REFERENCE_CACHE_SIZE = 8
_REFERENCES: OrderedDict[tuple[str, str, str], np.ndarray] = OrderedDict()


def reference_unitary(source: str, sha256: str, name: str) -> np.ndarray:
    """``Operator(input circuit).data``, remembering the last few circuits of this process."""
    key = (source, sha256, name)
    unitary = _REFERENCES.get(key)
    if unitary is None:
        from qiskit.quantum_info import Operator

        unitary = np.asarray(Operator(cached_input(source, sha256, name)).data)
        _REFERENCES[key] = unitary
        while len(_REFERENCES) > _REFERENCE_CACHE_SIZE:
            _REFERENCES.popitem(last=False)
    else:
        _REFERENCES.move_to_end(key)
    return unitary


def analyze_job(
    compiled_path: str,
    compiled_sha256: str,
    num_inputs: int,
    target_spec: Mapping[str, Any],
    exact_input: tuple[str, str, str] | None,
    max_active: int,
) -> dict[str, Any]:
    """Target validity, the qubits in play and, for pool A, the exact check (runs in a worker).

    ``exact_input`` is the input circuit's (path, SHA-256, name) when the exact check should
    run. ``active_ancillas`` counts active physical qubits that do not start with an input
    qubit (the QCEC hand-over needs it).
    """
    compiled = load_compiled(Path(compiled_path), compiled_sha256)
    problems, touched = scan_target(compiled, target_spec)
    result: dict[str, Any] = {"target_problems": problems, "physical_qubits": len(touched)}
    try:
        initial, final = layout_positions(compiled, num_inputs)
    except LayoutProblem as exc:
        problems.append(f"layout: {exc}")
        return result
    active = touched | set(initial) | set(final)
    result["active_qubits"] = len(active)
    result["active_ancillas"] = len(active - set(initial))
    if problems or exact_input is None:
        return result
    result["exact"] = exact_isometry_check(
        lambda: reference_unitary(*exact_input), compiled, initial, final, max_active
    )
    return result

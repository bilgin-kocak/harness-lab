"""Independent checks of a compiled circuit: target validity and the exact isometry check.

Equivalence (plan section B8): the input is the circuit after ``remove_final_measurements``;
the output is the compiled circuit with its layout. They are equivalent when, for every input
state of the circuit's qubits and with every other physical qubit starting in |0>, the compiled
circuit produces the input circuit's output, up to one global phase, on the physical qubits its
final layout names, with every other qubit back in |0>.

Target validity comes first: every operation must be a basis gate of the target (barriers and
delays are ignored) and every two-qubit gate must act on an edge of the coupling map. An output
that cannot run on the target fails, whatever it computes. Version 2 (plan amendment B15) reads
the circuit strictly, so that every checker sees the circuit Qiskit defines and nothing else:

* a basis gate must be that standard gate (``base_class``), a CZ controlled on |1>, with an rz
  angle that is a finite real number (an int, a float or a constant parameter expression); a
  barrier or delay must really be one (an instruction merely named ``barrier`` is outside the
  basis);
* the global phase must be a finite real number;
* the qubits must form exactly one register, in the circuit's own qubit order (QCEC numbers
  qubits by register, everything else by ``circuit.qubits``).

The checkers then read :func:`canonical_circuit`: the same qubits, registers and layout, every
gate rebuilt as a fresh standard gate with a float angle, barriers and delays (identities)
dropped, and a float global phase. QCEC's importer re-parses anything but a float angle and
drops the sign of a constant term; it cannot import a delay at all.

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

import io
import math
import numbers
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
    describe_compiled,
    layout_positions,
    read_verified,
)

EXACT_CHECK_ID = "exact-isometry/2"
TARGET_CHECK_ID = "target-validity/2"
FIDELITY_TOLERANCE = 1e-9
AMPLITUDE_TOLERANCE = 1e-7
# The counts of attempt.output that verify() compares with the payload (see the adapter).
COUNT_KEYS = (
    "two_qubit_gates",
    "depth",
    "size",
    "physical_qubits",
    "initial_layout",
    "final_layout",
)

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


def finite_real(value: Any) -> float | None:
    """``value`` as a float when it is a finite real number, else None.

    Accepted: Python and NumPy reals (bool and int included, as Qiskit reads them) and parameter
    expressions without free parameters that evaluate to a real number. Not accepted: complex
    numbers (``float`` of a NumPy complex silently drops the imaginary part), strings, free
    parameters, NaN and infinities.
    """
    from qiskit.circuit import ParameterExpression

    if isinstance(value, numbers.Real):
        result = float(value)
    elif isinstance(value, ParameterExpression):
        if value.parameters:
            return None
        try:
            number = value.numeric()
        except (TypeError, ValueError, RuntimeError):
            return None
        if isinstance(number, complex) and not isinstance(number, numbers.Real):
            if number.imag != 0:
                return None
            number = number.real
        if not isinstance(number, numbers.Real):
            return None
        result = float(number)
    else:
        return None
    return result if math.isfinite(result) else None


def _counts(counter: Counter[Any], limit: int = 5) -> str:
    items = counter.most_common()
    shown = [
        f"{'-'.join(map(str, key)) if isinstance(key, tuple) else key} x{count}"
        for key, count in items[:limit]
    ]
    return ", ".join(shown) + (", ..." if len(items) > limit else "")


def _standard_gates() -> dict[str, tuple[type, int, int]]:
    from qiskit.circuit.library import CZGate, RZGate, SXGate, XGate

    return {"cz": (CZGate, 2, 0), "rz": (RZGate, 1, 1), "sx": (SXGate, 1, 0), "x": (XGate, 1, 0)}


def _identities() -> tuple[type, ...]:
    from qiskit.circuit import Barrier, Delay

    return (Barrier, Delay)


def register_problem(circuit: Any) -> str | None:
    """Why the circuit's qubits are not exactly one register in circuit order (None if they are)."""
    registers = list(circuit.qregs)
    if len(registers) != 1 or list(registers[0]) != list(circuit.qubits):
        shape = ", ".join(f"{r.name}[{r.size}]" for r in registers) or "none"
        return (
            f"the qubits are not exactly one register in circuit order ({len(circuit.qubits)} "
            f"qubits; registers: {shape})"
        )
    return None


def scan_target(circuit: Any, spec: Mapping[str, Any]) -> tuple[list[str], set[int]]:
    """Why ``circuit`` cannot run on the target (empty when it can), and the qubits it touches.

    One pass over the circuit, so pool-B outputs with millions of gates are read once.
    """
    standard = _standard_gates()
    identities = _identities()
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
        if op.base_class in identities and not inst.clbits:
            continue
        qubits = tuple(index[q] for q in inst.qubits)
        touched.update(qubits)
        expected = allowed.get(name)
        if expected is None or inst.clbits:
            outside[name] += 1
            continue
        cls, arity, num_params = expected
        if (
            op.base_class is not cls
            or len(qubits) != arity
            or len(op.params) != num_params
            or getattr(op, "ctrl_state", 1) != 1
        ):
            impostors[name] += 1
            continue
        if arity == 2 and qubits not in edges:
            off_edge[qubits] += 1
        if num_params and finite_real(op.params[0]) is None:
            bad_angles += 1
    problems = []
    if circuit.num_qubits > size:
        problems.append(f"the circuit has {circuit.num_qubits} qubits, the target {size}")
    registers = register_problem(circuit)
    if registers is not None:
        problems.append(registers)
    if finite_real(circuit.global_phase) is None:
        problems.append(f"the global phase {circuit.global_phase!r} is not a finite real number")
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


def canonical_circuit(circuit: Any) -> Any:
    """The circuit every checker reads: a copy with standard gates, float angles, no identities.

    Same qubits, registers and layout (``copy_empty_like``); each CZ, rz, sx and x rebuilt as a
    fresh standard gate (rz with ``float`` of its angle), barriers and delays dropped, and the
    global phase as a float. Only for circuits :func:`scan_target` accepts; anything else
    raises ``ValueError``.
    """
    from qiskit.circuit import CircuitInstruction

    standard = {
        cls: (name, num_params) for name, (cls, _arity, num_params) in _standard_gates().items()
    }
    identities = _identities()
    if register_problem(circuit) is not None:
        raise ValueError("canonical_circuit: the qubits are not one register in circuit order")
    phase = finite_real(circuit.global_phase)
    if phase is None:
        raise ValueError("canonical_circuit: the global phase is not a finite real number")
    canonical = circuit.copy_empty_like()
    canonical.global_phase = phase
    if canonical.layout is not circuit.layout:  # copy_empty_like keeps it in Qiskit 2.6
        canonical._layout = circuit.layout
    for inst in circuit.data:
        op = inst.operation
        base = op.base_class
        if base in identities and not inst.clbits:
            continue
        known = standard.get(base)
        if known is None or inst.clbits or getattr(op, "ctrl_state", 1) != 1:
            raise ValueError(f"canonical_circuit: {op.name} is not a basis gate")
        name, num_params = known
        if len(op.params) != num_params:
            raise ValueError(f"canonical_circuit: {op.name} has {len(op.params)} parameters")
        if num_params:
            angle = finite_real(op.params[0])
            if angle is None:
                raise ValueError(f"canonical_circuit: {op.name} angle {op.params[0]!r}")
            gate = base(angle)
        else:
            gate = base()
        canonical._append(CircuitInstruction(gate, inst.qubits, ()))
    return canonical


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


def read_compiled(path: Path, sha256: str) -> tuple[Any | None, str | None]:
    """``(circuit, None)`` from a QPY file, or ``(None, why)`` when QPY cannot read it back.

    The file must match ``sha256`` (a mismatch raises: the harness, not the output, is wrong).
    """
    from qiskit import qpy

    data = read_verified(path, sha256)
    try:
        circuits = qpy.load(io.BytesIO(data))
    except Exception as exc:  # any reader failure: the output cannot be checked
        return None, f"QPY cannot read the compiled circuit back: {type(exc).__name__}: {exc}"
    if len(circuits) != 1:
        return None, f"the QPY file holds {len(circuits)} circuits, not one"
    return circuits[0], None


def analyze_job(
    compiled_path: str,
    compiled_sha256: str,
    num_inputs: int,
    target_spec: Mapping[str, Any],
    exact_input: tuple[str, str, str] | None,
    max_active: int,
) -> dict[str, Any]:
    """Target validity, the counts, the qubits in play and, for pool A, the exact check.

    Runs in a worker. ``exact_input`` is the input circuit's (path, SHA-256, name) when the
    exact check should run. ``counts`` are :func:`~.compile.describe_compiled` of the payload
    (what ``run`` reports in ``Attempt.output``); ``active_ancillas`` counts active physical
    qubits that do not start with an input qubit. ``load_error`` is set when QPY cannot read
    the file back. The exact check reads :func:`canonical_circuit`.
    """
    compiled, load_error = read_compiled(Path(compiled_path), compiled_sha256)
    if compiled is None:
        return {"load_error": load_error, "target_problems": []}
    problems, touched = scan_target(compiled, target_spec)
    result: dict[str, Any] = {"target_problems": problems, "physical_qubits": len(touched)}
    try:
        initial, final = layout_positions(compiled, num_inputs)
    except LayoutProblem as exc:
        problems.append(f"layout: {exc}")
        return result
    result["counts"] = describe_compiled(compiled, num_inputs)
    active = touched | set(initial) | set(final)
    result["active_qubits"] = len(active)
    result["active_ancillas"] = len(active - set(initial))
    if problems or exact_input is None:
        return result
    result["exact"] = exact_isometry_check(
        lambda: reference_unitary(*exact_input),
        canonical_circuit(compiled),
        initial,
        final,
        max_active,
    )
    return result

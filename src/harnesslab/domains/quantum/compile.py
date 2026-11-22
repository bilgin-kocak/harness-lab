"""Compiling one QASMBench circuit with one configuration, and the processes that do it.

The adapter never parses, compiles or checks a circuit in the event loop's process. CPU-bound
jobs run in :class:`WorkerPool`: a few long-lived Python processes, each in its own process
group, fed over pipes from threads (so the pool belongs to no event loop). A job that runs past
its time limit is killed with everything it started and only its worker is replaced;
``concurrent.futures.ProcessPoolExecutor`` cannot stop one job without breaking the whole pool.
Jobs take and return plain data: paths, hashes, the target spec, keyword arguments, bytes.

Every process derives the input circuit the same way (:func:`load_input_circuit`): check the
file's SHA-256, ``QuantumCircuit.from_qasm_file``, ``remove_final_measurements``, reject
anything non-unitary, and name the circuit after its file. Input circuits are never passed
between processes as QPY: QPY renames custom gates (``majority``, ``add4``, ...) and so does not
round-trip them exactly.

Two details make a compiled circuit's bytes depend on the circuit only
(:func:`serialize_compiled`). Qiskit assigns the ancilla labels of the layout to the unused
physical qubits in an order that changes between runs; ancillas are interchangeable, so they
are relabelled in physical order. And the default QPY version (18), written by Qiskit's Rust
writer, puts the layout's registers in an order that can change from one dump of the same
circuit to the next; version 16, written by the Python writer, orders them by physical qubit.
With both fixed, identical compilations gave identical bytes in every process and under every
hash seed tried.

This module imports only the standard library at import time; Qiskit is imported inside the
functions that need it.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import pickle
import queue
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# The basis of the study's target and the instructions every count and check ignores.
BASIS_GATES: tuple[str, ...] = ("cz", "rz", "sx", "x")
DIRECTIVES = frozenset({"barrier", "delay"})
NON_UNITARY = frozenset(
    {"measure", "reset", "if_else", "while_loop", "for_loop", "switch_case", "box"}
)
QPY_VERSION = 16

# The search space of plan section B5, in order: (name, levels, what it changes).
KNOBS: tuple[tuple[str, tuple[Any, ...], str], ...] = (
    (
        "optimization_level",
        (0, 1, 2, 3),
        "Preset pass manager level: 0 only maps to the target, 3 optimizes the most.",
    ),
    (
        "layout_method",
        ("default", "dense", "sabre", "trivial"),
        "Initial layout plugin; default leaves the choice to the optimization level.",
    ),
    (
        "routing_method",
        ("default", "basic", "lookahead", "sabre"),
        "Routing plugin; default leaves the choice to the optimization level.",
    ),
    (
        "translation_method",
        ("default", "synthesis", "translator"),
        "Basis translation plugin; default leaves the choice to the optimization level.",
    ),
    ("seed_transpiler", (0, 1, 2, 3, 4), "Seed of the transpiler's stochastic passes."),
    (
        "approximation_degree",
        (1.0, 0.99),
        "Synthesis approximation: 1.0 is exact; 0.99 allows approximate synthesis, which "
        "passes only if the output still verifies.",
    ),
    (
        "qubits_initially_zero",
        (True, False),
        "Whether synthesis may assume every qubit starts in |0> and so use qubits nothing has "
        "touched yet as clean ancillas (input qubits included; see the adapter tests).",
    ),
    (
        "hls_mcx",
        ("default", "noaux_v24", "gray_code"),
        "High-level synthesis plugin for multi-controlled X gates (HLSConfig(mcx=[...])).",
    ),
)
KNOB_NAMES: tuple[str, ...] = tuple(name for name, _, _ in KNOBS)

# Plan section B6: generate_preset_pass_manager(target=TARGET, seed_transpiler=0) with every
# other argument at its Qiskit 2.6.2 default.
BASELINE: dict[str, Any] = {
    "optimization_level": 2,
    "layout_method": "default",
    "routing_method": "default",
    "translation_method": "default",
    "seed_transpiler": 0,
    "approximation_degree": 1.0,
    "qubits_initially_zero": True,
    "hls_mcx": "default",
}


class NonUnitaryCircuit(ValueError):
    """The circuit still measures, resets or branches after its final measurements are removed."""


class LayoutProblem(ValueError):
    """A compiled circuit's layout does not place the input circuit's qubits."""


# --- configurations -------------------------------------------------------------------------


def pass_manager_kwargs(values: Mapping[str, Any]) -> dict[str, Any]:
    """The keyword arguments of ``generate_preset_pass_manager`` for one configuration.

    Everything except the target, in JSON form (``hls_config`` as ``{"mcx": [name]}``; see
    :func:`materialize_kwargs`). ``optimization_level``, ``seed_transpiler``,
    ``approximation_degree`` and ``qubits_initially_zero`` are always passed; the three
    ``*_method`` knobs only when they are not ``"default"``, and ``hls_config`` only when
    ``hls_mcx`` is not ``"default"``. Values outside the search grid are accepted when they are
    well formed (the robustness analysis uses seeds 1001-1005); Qiskit rejects unknown plugins.
    """
    missing = [name for name in KNOB_NAMES if name not in values]
    unknown = sorted(name for name in values if name not in KNOB_NAMES)
    if missing or unknown:
        raise ValueError(f"configuration: missing knobs {missing}, unknown knobs {unknown}")
    level = values["optimization_level"]
    if type(level) is not int or level not in (0, 1, 2, 3):
        raise ValueError(f"optimization_level must be 0, 1, 2 or 3, not {level!r}")
    seed = values["seed_transpiler"]
    if type(seed) is not int or seed < 0:
        raise ValueError(f"seed_transpiler must be a non-negative integer, not {seed!r}")
    degree = values["approximation_degree"]
    if isinstance(degree, bool) or not isinstance(degree, int | float) or not 0 < degree <= 1:
        raise ValueError(f"approximation_degree must be in (0, 1], not {degree!r}")
    zero = values["qubits_initially_zero"]
    if not isinstance(zero, bool):
        raise ValueError(f"qubits_initially_zero must be true or false, not {zero!r}")
    kwargs: dict[str, Any] = {
        "optimization_level": level,
        "seed_transpiler": seed,
        "approximation_degree": float(degree),
        "qubits_initially_zero": zero,
    }
    for name in ("layout_method", "routing_method", "translation_method", "hls_mcx"):
        method = values[name]
        if not isinstance(method, str) or not method:
            raise ValueError(f"{name} must be a plugin name, not {method!r}")
    for name in ("layout_method", "routing_method", "translation_method"):
        if values[name] != "default":
            kwargs[name] = values[name]
    if values["hls_mcx"] != "default":
        kwargs["hls_config"] = {"mcx": [values["hls_mcx"]]}
    return kwargs


def materialize_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the JSON form of :func:`pass_manager_kwargs` into Qiskit arguments."""
    result = dict(kwargs)
    hls = result.pop("hls_config", None)
    if hls is not None:
        from qiskit.transpiler.passes import HLSConfig

        result["hls_config"] = HLSConfig(**{gate: list(methods) for gate, methods in hls.items()})
    return result


def build_target(spec: Mapping[str, Any]) -> Any:
    """``Target.from_configuration`` with the spec's basis gates and directed edge list.

    Raises if the target does not have the number of qubits the spec records.
    """
    from qiskit.transpiler import CouplingMap, Target

    edges = [(int(a), int(b)) for a, b in spec["edges"]]
    target = Target.from_configuration(
        basis_gates=list(spec["basis_gates"]), coupling_map=CouplingMap(edges)
    )
    if target.num_qubits != int(spec["num_qubits"]):
        raise ValueError(
            f"the target built from the split has {target.num_qubits} qubits, "
            f"the split records {spec['num_qubits']}"
        )
    return target


# --- circuits -------------------------------------------------------------------------------


def sha256_hex(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def read_verified(path: Path, sha256: str) -> bytes:
    """The file's bytes, after checking them against the recorded SHA-256."""
    data = path.read_bytes()
    digest = sha256_hex(data)
    if digest != sha256:
        raise ValueError(f"{path}: SHA-256 {digest} does not match the recorded {sha256}")
    return data


def load_input_circuit(path: Path, sha256: str, name: str) -> Any:
    """The study's input circuit: parsed, final measurements removed, checked to be unitary.

    The file must match ``sha256``. The circuit is named ``name`` (the file's stem) so that its
    compiled versions do not carry Qiskit's process-wide ``circuit-<n>`` counter.
    """
    from qiskit import QuantumCircuit
    from qiskit.circuit import ControlFlowOp

    read_verified(path, sha256)
    parsed = QuantumCircuit.from_qasm_file(str(path))
    stripped = parsed.remove_final_measurements(inplace=False)
    left = sorted(
        {
            inst.operation.name
            for inst in stripped.data
            if inst.operation.name in NON_UNITARY
            or isinstance(inst.operation, ControlFlowOp)
            or inst.clbits
        }
    )
    if left:
        raise NonUnitaryCircuit(
            f"{name}: not unitary after removing final measurements: {', '.join(left)}"
        )
    stripped.name = name
    return stripped


def count_multi_qubit_gates(circuit: Any) -> int:
    return sum(
        1
        for inst in circuit.data
        if inst.operation.num_qubits >= 2 and inst.operation.name not in DIRECTIVES
    )


def layout_positions(circuit: Any, num_inputs: int) -> tuple[list[int], list[int]]:
    """Physical qubits holding each input qubit before (initial) and after (final) the circuit.

    Raises :class:`LayoutProblem` unless both lists place exactly ``num_inputs`` qubits on
    distinct physical qubits of the circuit. Without a layout the circuit must act on exactly
    the input's qubits, in order.
    """
    layout = circuit.layout
    if layout is None:
        if circuit.num_qubits != num_inputs:
            raise LayoutProblem(
                f"no layout, and the circuit has {circuit.num_qubits} qubits instead of the "
                f"input's {num_inputs}"
            )
        return list(range(num_inputs)), list(range(num_inputs))
    try:
        initial = [int(p) for p in layout.initial_index_layout(filter_ancillas=True)]
        final = [int(p) for p in layout.final_index_layout(filter_ancillas=True)]
    except Exception as exc:  # any failure to read the layout is a layout problem
        raise LayoutProblem(f"the layout cannot be read: {type(exc).__name__}: {exc}") from exc
    for label, positions in (("initial", initial), ("final", final)):
        if len(positions) != num_inputs:
            raise LayoutProblem(
                f"the {label} layout places {len(positions)} qubits, the input has {num_inputs}"
            )
        if len(set(positions)) != num_inputs:
            raise LayoutProblem(f"the {label} layout puts two qubits on one physical qubit")
        if any(p < 0 or p >= circuit.num_qubits for p in positions):
            raise LayoutProblem(f"the {label} layout names a physical qubit the circuit lacks")
    return initial, final


def describe_compiled(circuit: Any, num_inputs: int) -> dict[str, Any]:
    """The output record of one compilation: counts, depth and where the qubits are."""
    index = {bit: i for i, bit in enumerate(circuit.qubits)}
    two_qubit = 0
    touched: set[int] = set()
    for inst in circuit.data:
        if inst.operation.name in DIRECTIVES:
            continue
        if len(inst.qubits) == 2:
            two_qubit += 1
        touched.update(index[q] for q in inst.qubits)
    initial, final = layout_positions(circuit, num_inputs)
    return {
        "two_qubit_gates": two_qubit,
        "depth": circuit.depth(),
        "size": circuit.size(),
        "physical_qubits": len(touched),
        "initial_layout": initial,
        "final_layout": final,
    }


def canonicalize_ancillas(circuit: Any) -> None:
    """Relabel the layout's ancilla qubits in physical order, in place.

    Only the labels of the ancillas change: which physical qubits hold the input qubits, the
    final layout and the operations stay as they are. Layouts of an unexpected shape are left
    alone (the repeated-compilation check then reports any difference).
    """
    from qiskit.circuit import QuantumRegister
    from qiskit.transpiler import Layout
    from qiskit.transpiler.layout import TranspileLayout

    layout = circuit.layout
    if layout is None or layout.initial_layout is None or not layout.input_qubit_mapping:
        return
    num_inputs = layout._input_qubit_count
    if num_inputs is None:
        return
    ordered = sorted(layout.input_qubit_mapping.items(), key=lambda item: item[1])
    data = [bit for bit, index in ordered if index < num_inputs]
    if len(data) != num_inputs:
        return
    physical = [layout.initial_layout[bit] for bit in data]
    free = sorted(set(range(circuit.num_qubits)) - set(physical))
    if len(ordered) != num_inputs + len(free):
        return
    mapping = dict(zip(data, physical, strict=True))
    inputs = {bit: i for i, bit in enumerate(data)}
    ancillas = QuantumRegister(len(free), "ancilla") if free else None
    if ancillas is not None:
        for i, p in enumerate(free):
            mapping[ancillas[i]] = p
            inputs[ancillas[i]] = num_inputs + i
    initial = Layout(mapping)
    registers: list[Any] = []
    for bit in data:
        register = getattr(bit, "_register", None)
        if register is not None and register not in registers:
            registers.append(register)
    for register in registers:
        initial.add_register(register)
    if ancillas is not None:
        initial.add_register(ancillas)
    circuit._layout = TranspileLayout(
        initial_layout=initial,
        input_qubit_mapping=inputs,
        final_layout=layout.final_layout,
        _input_qubit_count=num_inputs,
        _output_qubit_list=layout._output_qubit_list,
    )


def serialize_compiled(circuit: Any) -> bytes:
    """QPY (version 16) of a compiled circuit with canonical ancilla labels (see module doc)."""
    from qiskit import qpy

    canonicalize_ancillas(circuit)
    buffer = io.BytesIO()
    qpy.dump(circuit, buffer, version=QPY_VERSION)
    return buffer.getvalue()


def load_compiled(path: Path, sha256: str) -> Any:
    """A compiled circuit from its QPY file, after checking the file's SHA-256."""
    from qiskit import qpy

    circuits = qpy.load(io.BytesIO(read_verified(path, sha256)))
    if len(circuits) != 1:
        raise ValueError(f"{path}: expected one circuit, found {len(circuits)}")
    return circuits[0]


# --- caches inside a worker process ---------------------------------------------------------

_INPUT_CACHE_SIZE = 8
_INPUTS: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
_TARGETS: dict[str, Any] = {}


def cached_input(source: str, sha256: str, name: str) -> Any:
    """:func:`load_input_circuit`, remembering the last few circuits of this process."""
    key = (source, sha256, name)
    circuit = _INPUTS.get(key)
    if circuit is None:
        circuit = load_input_circuit(Path(source), sha256, name)
        _INPUTS[key] = circuit
        while len(_INPUTS) > _INPUT_CACHE_SIZE:
            _INPUTS.popitem(last=False)
    else:
        _INPUTS.move_to_end(key)
    return circuit


def cached_target(spec: Mapping[str, Any]) -> Any:
    key = json.dumps(spec, sort_keys=True)
    target = _TARGETS.get(key)
    if target is None:
        target = _TARGETS[key] = build_target(spec)
    return target


# --- jobs -----------------------------------------------------------------------------------


def compile_job(
    source: str,
    sha256: str,
    name: str,
    target_spec: Mapping[str, Any],
    kwargs: Mapping[str, Any],
    repeats: int,
) -> dict[str, Any]:
    """Compile one circuit ``repeats`` times with one configuration (runs in a worker).

    Each repetition is timed with ``time.perf_counter`` around pass-manager construction and
    ``run``. The first output is kept; ``identical`` says whether every repetition serialized
    to the same bytes.
    """
    from qiskit.transpiler import generate_preset_pass_manager

    circuit = cached_input(source, sha256, name)
    target = cached_target(target_spec)
    options = materialize_kwargs(kwargs)
    times: list[float] = []
    first: bytes | None = None
    description: dict[str, Any] = {}
    identical = True
    for _ in range(repeats):
        start = time.perf_counter()
        manager = generate_preset_pass_manager(target=target, **options)
        compiled = manager.run(circuit)
        times.append(time.perf_counter() - start)
        if first is None:
            description = describe_compiled(compiled, circuit.num_qubits)
            first = serialize_compiled(compiled)
        elif serialize_compiled(compiled) != first:
            identical = False
        del compiled, manager
    if first is None:
        raise ValueError("compile_repeats must be at least 1")
    return {
        "qpy": first,
        "qpy_sha256": sha256_hex(first),
        "compile_times_s": times,
        "identical": identical,
        "output": description,
    }


def _resolve_job(name: str) -> Any:
    if name == "compile":
        return compile_job
    if name == "analyze":
        from harnesslab.domains.quantum.equivalence import analyze_job

        return analyze_job
    if name == "qcec":
        from harnesslab.domains.quantum.qcec_check import qcec_job

        return qcec_job
    raise KeyError(f"unknown job {name!r}")


# --- worker processes -----------------------------------------------------------------------

_HEADER = struct.Struct("!Q")
_BOOTSTRAP = (
    "import sys\n"
    "sys.path[:0] = sys.argv[1:]\n"
    "from harnesslab.domains.quantum.compile import worker_main\n"
    "worker_main()\n"
)
# Thread and parallelism settings a user may have set on purpose; nothing else is forwarded.
_ENV_PREFIXES = ("QISKIT_", "RAYON_", "OMP_", "OPENBLAS_", "MKL_")


class JobTimeout(TimeoutError):
    """A job ran past its time limit; its worker's process group was killed."""


class WorkerDied(RuntimeError):
    """A worker process exited while it was running a job."""


class WorkerJobError(RuntimeError):
    """A job raised inside its worker. ``kind`` is the exception type's name."""

    def __init__(self, kind: str, message: str, details: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.details = details


def kill_process_group(pid: int) -> None:
    """SIGKILL the process group led by ``pid`` (children start their own session)."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _worker_environment() -> dict[str, str]:
    from harnesslab.execution.process import build_child_env

    keep = [name for name in os.environ if name.startswith(_ENV_PREFIXES)]
    return build_child_env(include_auth=False, passthrough=keep)


def _package_root() -> str:
    import harnesslab

    return str(Path(harnesslab.__file__).resolve().parent.parent)


class _Worker:
    """One worker process and its pipes. Used by one thread at a time."""

    def __init__(self, log_path: Path | None) -> None:
        self.log_path = log_path
        log = open(log_path, "ab") if log_path is not None else subprocess.DEVNULL
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-c", _BOOTSTRAP, _package_root()],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                env=_worker_environment(),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            if log is not subprocess.DEVNULL:
                log.close()

    @property
    def pid(self) -> int:
        return self.process.pid

    def alive(self) -> bool:
        return self.process.poll() is None

    def call(self, job: str, args: tuple[Any, ...], timeout_s: float | None) -> Any:
        request = pickle.dumps((job, args), protocol=pickle.HIGHEST_PROTOCOL)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        stdin = self.process.stdin
        assert stdin is not None
        try:
            stdin.write(_HEADER.pack(len(request)))
            stdin.write(request)
            stdin.flush()
        except OSError as exc:
            raise self._died() from exc
        (size,) = _HEADER.unpack(self._read(_HEADER.size, deadline, timeout_s))
        status, value = pickle.loads(self._read(size, deadline, timeout_s))
        if status == "ok":
            return value
        kind, message, details = value
        raise WorkerJobError(kind, message, details)

    def _read(self, size: int, deadline: float | None, timeout_s: float | None) -> bytearray:
        stdout = self.process.stdout
        assert stdout is not None
        fd = stdout.fileno()
        buffer = bytearray(size)
        view = memoryview(buffer)
        got = 0
        while got < size:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    raise JobTimeout(f"the job did not finish within {timeout_s:g} s")
            n = os.readv(fd, [view[got:]])
            if n == 0:
                raise self._died()
            got += n
        return buffer

    def _died(self) -> WorkerDied:
        try:
            code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        if code is not None and code < 0:
            how = f"worker killed by {signal.Signals(-code).name}"
            if -code == signal.SIGKILL:  # the pool kills only on timeouts, reported elsewhere
                how += " (most likely the kernel's out-of-memory killer)"
        else:
            how = f"worker exited with code {code}"
        tail = ""
        if self.log_path is not None and self.log_path.exists():
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = " | ".join(lines[-5:])
        return WorkerDied(how + (f"; log: {tail}" if tail else ""))

    def kill(self) -> None:
        kill_process_group(self.process.pid)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


class WorkerPool:
    """A fixed number of worker processes for CPU-bound jobs, each killable on its own.

    ``await pool.call(job, *args, timeout_s=...)`` runs ``job`` (``"compile"``, ``"analyze"``
    or ``"qcec"``) in an idle worker. Past ``timeout_s`` the worker's process group is killed,
    :class:`JobTimeout` is raised and a fresh worker replaces it on the next call. Workers start
    on first use and run ``python -I`` with an environment that holds no credentials. Calls go
    through a thread per worker, so the pool works from any event loop.
    """

    def __init__(self, size: int, log_dir: Path | None = None) -> None:
        if size < 1:
            raise ValueError("a worker pool needs at least one worker")
        self.size = size
        self._log_dir = log_dir
        self._executor = ThreadPoolExecutor(max_workers=size, thread_name_prefix="hl-quantum")
        self._idle: queue.SimpleQueue[_Worker | None] = queue.SimpleQueue()
        for _ in range(size):
            self._idle.put(None)
        self._lock = threading.Lock()
        self._workers: set[_Worker] = set()
        self._started = 0
        self._closed = False

    async def call(self, job: str, *args: Any, timeout_s: float | None = None) -> Any:
        if self._closed:
            raise RuntimeError("the worker pool is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._call, job, args, timeout_s)

    def worker_pids(self) -> list[int]:
        with self._lock:
            return sorted(worker.pid for worker in self._workers if worker.alive())

    def _call(self, job: str, args: tuple[Any, ...], timeout_s: float | None) -> Any:
        worker = self._idle.get()
        try:
            if worker is not None and not worker.alive():
                self._discard(worker)
                worker = None
            if worker is None:
                worker = self._spawn()
            try:
                return worker.call(job, args, timeout_s)
            except (JobTimeout, WorkerDied):
                self._discard(worker)
                worker = None
                raise
        finally:
            self._idle.put(worker)

    def _spawn(self) -> _Worker:
        with self._lock:
            if self._closed:
                raise RuntimeError("the worker pool is closed")
            self._started += 1
            log = None
            if self._log_dir is not None:
                log = self._log_dir / f"worker-{self._started}.log"
            worker = _Worker(log)
            self._workers.add(worker)
            return worker

    def _discard(self, worker: _Worker) -> None:
        worker.kill()
        with self._lock:
            self._workers.discard(worker)

    def close(self) -> None:
        """Kill every worker (and anything it started) and stop accepting calls."""
        with self._lock:
            self._closed = True
            workers = list(self._workers)
            self._workers.clear()
        for worker in workers:
            worker.kill()
        self._executor.shutdown(wait=False, cancel_futures=True)


def _read_exactly(stream: Any, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            return None
        chunks += chunk
    return bytes(chunks)


def _exit_when_orphaned(parent: int) -> None:
    while True:
        time.sleep(1.0)
        if os.getppid() != parent:
            os._exit(3)


def _first_to_go_when_memory_runs_out() -> None:
    """Raise this process's OOM score to the maximum (Linux; raising needs no privilege).

    QCEC on a circuit with millions of gates can exhaust the machine's memory (it did on
    medium/vqe_n24); the kernel should then kill a worker, never the study or another program.
    """
    try:
        with open("/proc/self/oom_score_adj", "w", encoding="ascii") as handle:
            handle.write("1000")
    except OSError:
        pass


def worker_main() -> None:
    """The loop of one worker process: read a job, run it, write the result, until EOF.

    The protocol uses a private copy of stdout; file descriptor 1 is pointed at stderr, so
    output printed by Qiskit, QCEC or C++ code ends in the worker's log instead of corrupting
    the protocol. The worker exits if its parent goes away, and is the kernel's first choice
    when memory runs out.
    """
    channel = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    _first_to_go_when_memory_runs_out()
    threading.Thread(target=_exit_when_orphaned, args=(os.getppid(),), daemon=True).start()
    requests = sys.stdin.buffer
    while True:
        header = _read_exactly(requests, _HEADER.size)
        if header is None:
            return
        body = _read_exactly(requests, _HEADER.unpack(header)[0])
        if body is None:
            return
        job, args = pickle.loads(body)
        try:
            reply: tuple[str, Any] = ("ok", _resolve_job(job)(*args))
        except Exception as exc:  # every job failure is reported to the parent
            reply = ("error", (type(exc).__name__, str(exc), traceback.format_exc()))
        data = pickle.dumps(reply, protocol=pickle.HIGHEST_PROTOCOL)
        channel.write(_HEADER.pack(len(data)))
        channel.write(data)
        channel.flush()

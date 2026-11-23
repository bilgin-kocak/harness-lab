"""The ``qiskit-qasmbench`` domain adapter: Qiskit 2.6.2 compiling QASMBench circuits.

Part B of ``docs/plans/2026-11-22-math-quantum-experiment-plan.md`` on the contract of
:mod:`harnesslab.study.contract`:

``load_tasks``
    Circuits ``<size>/<name>`` of a QASMBench checkout at the pinned commit (``git rev-parse
    HEAD`` must match). Each file's SHA-256 must equal the one in the study's ``split.json``,
    which also gives each circuit's pool and the fixed target. A circuit is parsed with
    ``QuantumCircuit.from_qasm_file`` and its final measurements removed; one that still
    measures, resets or branches is rejected.
``search_space`` / ``baseline_config``
    The knobs of plan section B5 and the baseline of B6 (:data:`compile.KNOBS`).
``run``
    ``generate_preset_pass_manager`` on the split's target, ``compile_repeats`` times in a
    worker process; the compile time is their median. The compiled circuit travels as QPY in
    ``Attempt.payload``; ``Attempt.output`` holds its hash, counts and layouts. Costs nothing.
``verify``
    Target validity, then the exact check (pool A, :mod:`.equivalence`) or QCEC (pool B,
    :mod:`.qcec_check`), both on the payload's canonical form. On pool A QCEC also runs as a
    cross-check; a pass/fail disagreement inside the checkers' tolerance gap is unverifiable,
    any other raises :class:`CheckerDisagreement`. A payload QPY cannot read back, or whose
    analysis passes ``analyze_timeout_s``, is unverifiable. Verdicts are cached in memory by
    task, output hash and checker versions; an attempt whose ``output`` misreports the
    payload's counts fails (checked outside the cache).
``metrics``
    Two-qubit gates, depth, size and physical qubits counted from the verified payload (not
    from ``Attempt.output``), compile time, active qubits.

Options, all optional: ``qasmbench_dir`` (else ``$HARNESSLAB_QASMBENCH_DIR``, else
``~/.cache/harnesslab/datasets/QASMBench@<commit>``), ``commit``, ``fetch`` (clone the pinned
commit when the directory is missing), ``split_file`` (``<study>/split.json``),
``exact_max_active_qubits`` (14, at most 16: the exact check holds 2^(k+n) amplitudes),
``qcec_timeout_s`` (60), ``qcec_kill_s`` (90),
``qcec_on_pool_a`` (true), ``compile_repeats`` (3), ``compile_timeout_s``,
``analyze_timeout_s`` (1200) and ``workers`` (2). Relative paths in options are relative to the
study directory.

``compile_timeout_s`` bounds all repetitions of one attempt together; past it the worker is
killed and the attempt is a ``timeout``. The plan sets no such limit. The default, 1200 s, only
guards against hangs: the largest dev circuit (medium/vqe_n24, 10.8 million gates once compiled)
needs about 440 s for three baseline compilations here, while lookahead routing did not finish
within 30 s on most circuits tried. A study should set its own value; null means no limit.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from harnesslab.domains.quantum import qcec_check
from harnesslab.domains.quantum.compile import (
    BASELINE,
    BASIS_GATES,
    KNOBS,
    QPY_VERSION,
    JobTimeout,
    WorkerDied,
    WorkerJobError,
    WorkerPool,
    build_target,
    count_multi_qubit_gates,
    load_input_circuit,
    pass_manager_kwargs,
    sha256_hex,
)
from harnesslab.domains.quantum.equivalence import COUNT_KEYS, EXACT_CHECK_ID, TARGET_CHECK_ID
from harnesslab.execution.process import build_child_env
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapterBase,
    Knob,
    Metrics,
    RunContext,
    SearchSpace,
    Task,
    Verdict,
    VerdictStatus,
    VerifyContext,
    sha256_json,
)

ADAPTER_NAME = "qiskit-qasmbench"
ADAPTER_VERSION = "2"
DEFAULT_COMMIT = "357b942396d5c2b7cbc1c229c585a6ef5ccaebac"
REPO_URL = "https://github.com/pnnl/QASMBench"
DATASET_ENV = "HARNESSLAB_QASMBENCH_DIR"
POOLS = ("A", "B")
METRIC_KEYS = (
    "two_qubit_gates",
    "depth",
    "size",
    "compile_time_s",
    "active_qubits",
    "physical_qubits",
)
NO_OUTPUT_CHECKER = f"{ADAPTER_NAME}/{ADAPTER_VERSION} no-output"
RECORD_CHECKER = f"{ADAPTER_NAME}/{ADAPTER_VERSION} output-record"
# A pool-A pass/fail disagreement whose exact-check deviation lies in this range is explained by
# the checkers' different tolerances (plan amendment B15): unverifiable, not a stop. Below it the
# output is numerically exact; above it no tolerance explains QCEC passing it.
DISAGREEMENT_GAP = (1e-11, 1e-5)
_ID = re.compile(r"^(?P<size>small|medium|large)/(?P<name>[A-Za-z0-9][A-Za-z0-9_.+-]*)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_OPTION_NAMES = (
    "qasmbench_dir",
    "commit",
    "fetch",
    "split_file",
    "exact_max_active_qubits",
    "qcec_timeout_s",
    "qcec_kill_s",
    "qcec_on_pool_a",
    "compile_repeats",
    "compile_timeout_s",
    "analyze_timeout_s",
    "workers",
)


class CheckerDisagreement(RuntimeError):
    """The exact check and QCEC disagree (one passes, the other fails) on a pool-A output, and
    their tolerances cannot explain it.

    The plan stops the study until such a disagreement is explained, so ``verify`` raises.
    A disagreement whose exact-check deviation lies in :data:`DISAGREEMENT_GAP` is explained
    (the exact check bounds every amplitude, QCEC a normalized Frobenius distance) and gives an
    unverifiable verdict instead (plan amendment B15). ``exact`` and ``qcec`` hold both results.
    """

    def __init__(
        self, task_id: str, qpy_sha256: str, exact: dict[str, Any], qcec: dict[str, Any]
    ) -> None:
        super().__init__(
            f"{task_id} (output {qpy_sha256[:12]}): the exact check says {exact['status']} "
            f"({exact.get('reason')}) but QCEC says {qcec['status']} ({qcec.get('reason')})"
        )
        self.task_id = task_id
        self.qpy_sha256 = qpy_sha256
        self.exact = exact
        self.qcec = qcec


# --- options --------------------------------------------------------------------------------


def _resolve(value: Any, key: str, study_dir: Path) -> Path:
    if not isinstance(value, str | os.PathLike) or not str(value):
        raise ValueError(f"option {key} must be a path, not {value!r}")
    path = Path(value).expanduser()
    return path if path.is_absolute() else study_dir / path


def _integer(options: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = options.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"option {key} must be an integer from {low} to {high}, not {value!r}")
    return value


def _seconds(options: dict[str, Any], key: str, default: float) -> float:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"option {key} must be a number of seconds, not {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"option {key} must be positive and finite, not {value!r}")
    return float(value)


def _flag(options: dict[str, Any], key: str, default: bool) -> bool:
    value = options.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"option {key} must be true or false, not {value!r}")
    return value


@dataclass(frozen=True)
class QuantumOptions:
    """The adapter's options, checked and with defaults filled in."""

    qasmbench_dir: Path
    commit: str
    fetch: bool
    split_file: Path
    exact_max_active_qubits: int
    qcec_timeout_s: float
    qcec_kill_s: float
    qcec_on_pool_a: bool
    compile_repeats: int
    compile_timeout_s: float | None
    analyze_timeout_s: float
    workers: int

    @classmethod
    def parse(cls, options: dict[str, Any], study_dir: Path) -> QuantumOptions:
        unknown = sorted(set(options) - set(_OPTION_NAMES))
        if unknown:
            raise ValueError(f"unknown options {unknown} (known: {', '.join(_OPTION_NAMES)})")
        commit = options.get("commit", DEFAULT_COMMIT)
        if not isinstance(commit, str) or not _COMMIT.match(commit):
            raise ValueError(f"option commit must be a full 40-character SHA, not {commit!r}")
        if options.get("qasmbench_dir") is not None:
            dataset = _resolve(options["qasmbench_dir"], "qasmbench_dir", study_dir)
        elif os.environ.get(DATASET_ENV):
            dataset = Path(os.environ[DATASET_ENV]).expanduser().absolute()
        else:
            dataset = Path.home() / ".cache" / "harnesslab" / "datasets" / f"QASMBench@{commit}"
        split = options.get("split_file")
        if split is None:
            split_file = study_dir / "split.json"
        else:
            split_file = _resolve(split, "split_file", study_dir)
        compile_timeout = None
        if options.get("compile_timeout_s", 1200.0) is not None:
            compile_timeout = _seconds(options, "compile_timeout_s", 1200.0)
        return cls(
            qasmbench_dir=dataset,
            commit=commit,
            fetch=_flag(options, "fetch", False),
            split_file=split_file,
            exact_max_active_qubits=_integer(options, "exact_max_active_qubits", 14, 1, 16),
            qcec_timeout_s=_seconds(options, "qcec_timeout_s", 60.0),
            qcec_kill_s=_seconds(options, "qcec_kill_s", 90.0),
            qcec_on_pool_a=_flag(options, "qcec_on_pool_a", True),
            compile_repeats=_integer(options, "compile_repeats", 3, 1, 25),
            compile_timeout_s=compile_timeout,
            analyze_timeout_s=_seconds(options, "analyze_timeout_s", 1200.0),
            workers=_integer(options, "workers", 2, 1, 64),
        )


# --- records and resources ------------------------------------------------------------------


@dataclass(frozen=True)
class CircuitRecord:
    """One loaded task: where its file is and what the split and the parse say about it."""

    id: str
    file: str  # relative to the QASMBench checkout
    path: Path
    sha256: str
    name: str
    qubits: int
    pool: str
    multi_qubit_gates: int


class _Resources:
    """The worker pool and the scratch directory, released by ``close()`` or at collection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.pool: WorkerPool | None = None
        self.scratch: Path | None = None

    def scratch_dir(self) -> Path:
        with self._lock:
            if self.scratch is None:
                self.scratch = Path(tempfile.mkdtemp(prefix="harnesslab-quantum-"))
            return self.scratch

    def worker_pool(self, size: int) -> WorkerPool:
        with self._lock:
            if self.pool is None:
                self.pool = WorkerPool(size, log_dir=self.scratch_dir())
            return self.pool

    def release(self) -> None:
        with self._lock:
            pool, scratch = self.pool, self.scratch
            self.pool = None
            self.scratch = None
        if pool is not None:
            pool.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.partial")
    partial.write_bytes(data)
    partial.replace(path)


def _git(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=build_child_env(include_auth=False),
    )


def _against_output(verdict: Verdict, attempt: Attempt) -> Verdict:
    """``verdict``, failed when ``attempt.output`` misreports its own compiled circuit.

    The counts in ``attempt.output`` come from ``run``; the verdict's ``counts`` from the
    payload the checkers read. A pass whose output disagrees on any count it reports becomes a
    fail (metrics are taken from the payload either way). Not cached: two attempts can share a
    payload but not their output records.
    """
    counts = verdict.details.get("counts")
    if not isinstance(counts, dict):
        return verdict
    wrong = [
        f"{key} {attempt.output[key]!r} (the circuit has {counts.get(key)!r})"
        for key in COUNT_KEYS
        if key in attempt.output and attempt.output[key] != counts.get(key)
    ]
    if not wrong:
        return verdict
    details = verdict.details | {"output_mismatch": wrong}
    if verdict.status != VerdictStatus.PASS:
        return verdict.model_copy(update={"details": details})
    return verdict.model_copy(
        update={
            "status": VerdictStatus.FAIL,
            "reason": "the attempt's output misreports its compiled circuit: " + "; ".join(wrong),
            "checker": RECORD_CHECKER,
            "details": details | {"decided_by": "output-record"},
        }
    )


def _reused(verdict: Verdict, started: float) -> Verdict:
    copy = verdict.model_copy(deep=True)
    copy.details["cached"] = True
    copy.duration_s = time.perf_counter() - started
    return copy


# --- the adapter ----------------------------------------------------------------------------


class QiskitQasmBenchAdapter(DomainAdapterBase):
    """Qiskit compilation of QASMBench circuits onto the study's fixed target."""

    name = ADAPTER_NAME
    version = ADAPTER_VERSION
    paid = False

    def __init__(self, options: dict[str, Any] | None, study_dir: Path) -> None:
        import numpy
        import qiskit

        self.study_dir = Path(study_dir)
        self.options = QuantumOptions.parse(dict(options or {}), self.study_dir)
        raw = self.options.split_file.read_bytes()
        self.split_sha256 = sha256_hex(raw)
        split = json.loads(raw)
        self.qiskit_version = qiskit.__version__
        self.numpy_version = numpy.__version__
        self._check_split(split)
        target = split["target"]
        self.target_spec: dict[str, Any] = {
            "basis_gates": list(target["basis_gates"]),
            "edges": [[int(a), int(b)] for a, b in target["edges"]],
            "num_qubits": int(target["num_qubits"]),
        }
        self.target_sha256 = sha256_json(self.target_spec)
        self.target = build_target(self.target_spec)  # raises unless it has num_qubits qubits
        self._rows: dict[str, dict[str, Any]] = {row["id"]: row for row in split["circuits"]}
        self.exact_checker = f"{EXACT_CHECK_ID} numpy {self.numpy_version}"
        self._checker_key = "|".join(
            [
                TARGET_CHECK_ID,
                self.exact_checker,
                f"mqt.qcec {qcec_check.qcec_version()}",
                f"qiskit {self.qiskit_version}",
                f"max_active={self.options.exact_max_active_qubits}",
                f"qcec_timeout={self.options.qcec_timeout_s:g}",
                f"qcec_kill={self.options.qcec_kill_s:g}",
                f"qcec_on_pool_a={self.options.qcec_on_pool_a}",
            ]
        )
        self._records: dict[str, CircuitRecord] = {}
        self._parsed: dict[str, Any] = {}
        self._load_lock = threading.RLock()
        self._dataset_ready = False
        self._verdicts: dict[tuple[str, str, str], Verdict] = {}
        self._pending: dict[tuple[str, str, str], asyncio.Task[Verdict]] = {}
        self._resources = _Resources()
        self._finalizer = weakref.finalize(self, self._resources.release)

    def _check_split(self, split: dict[str, Any]) -> None:
        where = self.options.split_file
        recorded = split.get("dataset", {}).get("commit")
        if recorded is not None and recorded != self.options.commit:
            raise ValueError(f"{where} was made at QASMBench {recorded}, not {self.options.commit}")
        pinned = split.get("qiskit")
        if pinned is not None and pinned != self.qiskit_version:
            raise RuntimeError(
                f"{where} pins qiskit {pinned}, but {self.qiskit_version} is installed"
            )
        target = split.get("target")
        if not isinstance(target, dict) or not {"basis_gates", "edges", "num_qubits"} <= set(
            target
        ):
            raise ValueError(f"{where}: target needs basis_gates, edges and num_qubits")
        if sorted(target["basis_gates"]) != sorted(BASIS_GATES):
            raise ValueError(
                f"{where}: the checks support the basis {list(BASIS_GATES)}, "
                f"not {target['basis_gates']}"
            )
        if not isinstance(split.get("circuits"), list):
            raise ValueError(f"{where}: no circuits list")

    # -- tasks ---------------------------------------------------------------------------

    def load_tasks(self, ids: Sequence[str]) -> list[Task]:
        ids = list(ids)
        duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate task ids: {duplicates}")
        tasks = []
        for task_id in ids:
            record = self._load(task_id)
            tasks.append(
                Task(
                    id=task_id,
                    content_hash=record.sha256,
                    data={
                        "file": record.file,
                        "qubits": record.qubits,
                        "pool": record.pool,
                        "multi_qubit_gates": record.multi_qubit_gates,
                    },
                    tags=(record.pool,),
                )
            )
        return tasks

    def _load(self, task_id: str) -> CircuitRecord:
        with self._load_lock:
            record = self._records.get(task_id)
            if record is None:
                record, circuit = self._read_circuit(task_id)
                self._records[task_id] = record
                self._parsed[task_id] = circuit
            return record

    def _row(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not _ID.match(task_id):
            raise ValueError(f"{task_id!r} is not a QASMBench circuit id like 'small/adder_n10'")
        row = self._rows.get(task_id)
        if row is None:
            raise ValueError(f"{task_id} is not in {self.options.split_file}")
        if row.get("status") != "eligible":
            raise ValueError(f"{task_id} is not eligible in the split: {row.get('reason')}")
        if row.get("pool") not in POOLS or not isinstance(row.get("sha256"), str):
            raise ValueError(f"{task_id}: the split gives no pool or SHA-256")
        return row

    def _read_circuit(self, task_id: str) -> tuple[CircuitRecord, Any]:
        row = self._row(task_id)
        with self._load_lock:
            self._ensure_dataset()
        size, name = task_id.split("/")
        relative = f"{size}/{name}/{name}.qasm"
        if row.get("file") != relative:
            raise ValueError(f"{task_id}: the split names {row.get('file')!r}, expected {relative}")
        root = self.options.qasmbench_dir.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"{task_id}: {relative} resolves outside {root}")
        circuit = load_input_circuit(path, row["sha256"], name)
        multi = count_multi_qubit_gates(circuit)
        if circuit.num_qubits != row.get("qubits") or multi != row.get("multi_qubit_gates"):
            raise ValueError(
                f"{task_id}: parsed {circuit.num_qubits} qubits and {multi} multi-qubit gates, "
                f"the split records {row.get('qubits')} and {row.get('multi_qubit_gates')}"
            )
        record = CircuitRecord(
            id=task_id,
            file=relative,
            path=path,
            sha256=row["sha256"],
            name=name,
            qubits=circuit.num_qubits,
            pool=row["pool"],
            multi_qubit_gates=multi,
        )
        return record, circuit

    def _ensure_dataset(self) -> None:
        if self._dataset_ready:
            return
        directory = self.options.qasmbench_dir
        if not directory.exists():
            if not self.options.fetch:
                raise FileNotFoundError(
                    f"no QASMBench checkout at {directory}: set the qasmbench_dir option or "
                    f"{DATASET_ENV}, or set fetch: true to clone {REPO_URL} at "
                    f"{self.options.commit}"
                )
            self._fetch(directory)
        result = _git("-C", str(directory), "rev-parse", "HEAD", timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"cannot read the commit of {directory}: {result.stderr.strip()}")
        head = result.stdout.strip()
        if head != self.options.commit:
            raise RuntimeError(
                f"{directory} is at commit {head}, the study pins {self.options.commit}"
            )
        self._dataset_ready = True

    def _fetch(self, directory: Path) -> None:
        directory.parent.mkdir(parents=True, exist_ok=True)
        partial = directory.with_name(f"{directory.name}.partial-{os.getpid()}")
        shutil.rmtree(partial, ignore_errors=True)
        try:
            for args, timeout in (
                (("clone", "--quiet", REPO_URL, str(partial)), 3600),
                (("-C", str(partial), "checkout", "--quiet", "--detach", self.options.commit), 600),
            ):
                result = _git(*args, timeout=timeout)
                if result.returncode != 0:
                    raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
            partial.rename(directory)
        finally:
            shutil.rmtree(partial, ignore_errors=True)

    async def _record_for(self, task: Task) -> CircuitRecord:
        record = self._records.get(task.id)
        if record is None:
            record = await asyncio.to_thread(self._load, task.id)
        if task.content_hash != record.sha256:
            raise ValueError(
                f"{task.id}: content hash {task.content_hash} differs from the split's "
                f"{record.sha256}"
            )
        return record

    # -- search space --------------------------------------------------------------------

    def search_space(self) -> SearchSpace:
        return SearchSpace(
            knobs=tuple(
                Knob(name=name, levels=levels, description=description)
                for name, levels, description in KNOBS
            )
        )

    def baseline_config(self) -> Config:
        return Config(values=dict(BASELINE), label="baseline")

    # -- run -----------------------------------------------------------------------------

    def _pool(self) -> WorkerPool:
        return self._resources.worker_pool(self.options.workers)

    async def run(self, task: Task, config: Config, ctx: RunContext) -> Attempt:
        started = time.perf_counter()
        record = await self._record_for(task)
        kwargs = pass_manager_kwargs(config.values)
        details: dict[str, Any] = {"pass_manager_kwargs": kwargs, "qiskit": self.qiskit_version}

        def attempt(status: AttemptStatus, **fields: Any) -> Attempt:
            return Attempt(
                task_id=task.id,
                config_hash=config.config_hash,
                repetition=ctx.repetition,
                status=status,
                wall_time_s=time.perf_counter() - started,
                details=details,
                **fields,
            )

        timeout = self.options.compile_timeout_s
        try:
            result = await self._pool().call(
                "compile",
                str(record.path),
                record.sha256,
                record.name,
                self.target_spec,
                kwargs,
                self.options.compile_repeats,
                timeout_s=timeout,
            )
        except JobTimeout:
            return attempt(
                AttemptStatus.TIMEOUT,
                error=f"compilation did not finish within compile_timeout_s = {timeout:g} s",
            )
        except WorkerDied as exc:
            return attempt(AttemptStatus.ERROR, error=f"the compiler process died: {exc}")
        except WorkerJobError as exc:
            details["traceback"] = exc.details.splitlines()[-20:]
            return attempt(AttemptStatus.ERROR, error=str(exc))
        times = [float(t) for t in result["compile_times_s"]]
        details.update(
            compile_times_s=times,
            compile_time_s=statistics.median(times),
            nondeterministic=not result["identical"],
            qpy_version=QPY_VERSION,
            qpy_bytes=len(result["qpy"]),
        )
        if ctx.keep_artifacts:
            await asyncio.to_thread(
                _write_atomic, Path(ctx.attempt_dir) / "compiled.qpy", result["qpy"]
            )
            details["artifact"] = "compiled.qpy"
        return attempt(
            AttemptStatus.COMPLETED,
            output={"qpy_sha256": result["qpy_sha256"], **result["output"]},
            payload=result["qpy"],
        )

    # -- verify --------------------------------------------------------------------------

    async def verify(self, task: Task, attempt: Attempt, ctx: VerifyContext) -> Verdict:
        started = time.perf_counter()
        record = await self._record_for(task)
        if attempt.status != AttemptStatus.COMPLETED:
            reason = f"no compiled circuit: the attempt ended with status {attempt.status.value}"
            if attempt.error:
                reason += f" ({attempt.error})"
            return Verdict(
                status=VerdictStatus.FAIL,
                reason=reason,
                checker=NO_OUTPUT_CHECKER,
                details={"pool": record.pool, "decided_by": "no-output", "cached": False},
                duration_s=time.perf_counter() - started,
            )
        payload = await self._payload(attempt, ctx)
        digest = attempt.output["qpy_sha256"]
        key = (task.id, digest, self._checker_key)
        known = self._verdicts.get(key)
        if known is not None:
            return _against_output(_reused(known, started), attempt)
        loop = asyncio.get_running_loop()
        pending = self._pending.get(key)
        if pending is not None and pending.get_loop() is loop:
            return _against_output(_reused(await asyncio.shield(pending), started), attempt)
        check = loop.create_task(self._check(record, digest, payload))
        self._pending[key] = check
        check.add_done_callback(functools.partial(self._settle, key))
        verdict = await asyncio.shield(check)
        copy = verdict.model_copy(deep=True)
        copy.duration_s = time.perf_counter() - started
        return _against_output(copy, attempt)

    def _settle(self, key: tuple[str, str, str], check: asyncio.Task[Verdict]) -> None:
        if self._pending.get(key) is check:
            del self._pending[key]
        if check.cancelled():
            return
        if check.exception() is None:  # a raised check is never cached
            self._verdicts[key] = check.result()

    async def _payload(self, attempt: Attempt, ctx: VerifyContext) -> bytes:
        expected = attempt.output.get("qpy_sha256")
        if not isinstance(expected, str):
            raise RuntimeError(f"{attempt.task_id}: a completed attempt without output.qpy_sha256")
        data = attempt.payload
        if data is None:
            artifact = Path(ctx.attempt_dir) / "compiled.qpy"
            if not artifact.is_file():
                raise RuntimeError(
                    f"{attempt.task_id}: no compiled circuit to verify (no payload in memory "
                    f"and no {artifact})"
                )
            data = await asyncio.to_thread(artifact.read_bytes)
        if not isinstance(data, bytes | bytearray):
            raise TypeError(f"{attempt.task_id}: the payload is {type(data).__name__}, not bytes")
        digest = await asyncio.to_thread(sha256_hex, data)
        if digest != expected:
            raise RuntimeError(
                f"{attempt.task_id}: the compiled circuit's SHA-256 {digest} does not match "
                f"output.qpy_sha256 {expected}"
            )
        return bytes(data)

    def _stage(self, digest: str, payload: bytes) -> Path:
        path = self._resources.scratch_dir() / f"compiled-{digest[:16]}-{uuid.uuid4().hex[:8]}.qpy"
        _write_atomic(path, payload)
        return path

    async def _analyze(self, record: CircuitRecord, path: Path, digest: str) -> dict[str, Any]:
        exact_input = (str(record.path), record.sha256, record.name) if record.pool == "A" else None
        args = (
            str(path),
            digest,
            record.qubits,
            self.target_spec,
            exact_input,
            self.options.exact_max_active_qubits,
        )
        limit = self.options.analyze_timeout_s
        try:
            try:
                return await self._pool().call("analyze", *args, timeout_s=limit)
            except WorkerDied:  # one retry: a worker can die for reasons unrelated to it
                return await self._pool().call("analyze", *args, timeout_s=limit)
        except JobTimeout:
            return {
                "timeout": (
                    "the analysis (target validity, counts and the exact check) did not finish "
                    f"within analyze_timeout_s = {limit:g} s; its process was killed"
                ),
                "target_problems": [],
            }

    async def _check(self, record: CircuitRecord, digest: str, payload: bytes) -> Verdict:
        started = time.perf_counter()
        path = await asyncio.to_thread(self._stage, digest, payload)
        try:
            analysis = await self._analyze(record, path, digest)
            problems = analysis["target_problems"]
            details: dict[str, Any] = {
                "pool": record.pool,
                "cached": False,
                "active_qubits": analysis.get("active_qubits"),
                "active_ancillas": analysis.get("active_ancillas"),
                "physical_qubits": analysis.get("physical_qubits"),
                "target_problems": problems,
                "counts": analysis.get("counts"),
            }

            def verdict(status: VerdictStatus, reason: str, checker: str, by: str) -> Verdict:
                return Verdict(
                    status=status,
                    reason=reason,
                    checker=checker,
                    details=details | {"decided_by": by},
                    duration_s=time.perf_counter() - started,
                )

            for key, by in (("load_error", "load"), ("timeout", "analysis-timeout")):
                if key in analysis:
                    return verdict(VerdictStatus.UNVERIFIABLE, analysis[key], TARGET_CHECK_ID, by)
            if problems:
                return verdict(
                    VerdictStatus.FAIL,
                    "cannot run on the target: " + "; ".join(problems),
                    TARGET_CHECK_ID,
                    "target",
                )
            exact = analysis.get("exact")
            if exact is not None:
                details["exact"] = exact
            qcec = None
            if record.pool == "B" or self.options.qcec_on_pool_a:
                qcec = await qcec_check.run_qcec(
                    self._pool(),
                    source=str(record.path),
                    sha256=record.sha256,
                    name=record.name,
                    compiled_path=str(path),
                    compiled_sha256=digest,
                    timeout_s=self.options.qcec_timeout_s,
                    kill_after_s=self.options.qcec_kill_s,
                )
                details["qcec"] = qcec
            if record.pool == "A":
                if exact is None:
                    raise RuntimeError(f"{record.id}: the exact check did not run")
                if qcec is not None and {exact["status"], qcec["status"]} == {"pass", "fail"}:
                    deviation = exact.get("max_deviation")
                    low, high = DISAGREEMENT_GAP
                    if deviation is None or not low <= deviation <= high:
                        raise CheckerDisagreement(record.id, digest, exact, qcec)
                    return verdict(
                        VerdictStatus.UNVERIFIABLE,
                        (
                            f"the checkers disagree inside their tolerance gap: the exact check "
                            f"says {exact['status']} (largest deviation {deviation:.2e}), QCEC "
                            f"says {qcec['status']} ({qcec['reason']}); not counted as verified"
                        ),
                        self.exact_checker,
                        "disagreement",
                    )
                return verdict(
                    VerdictStatus(exact["status"]), exact["reason"], self.exact_checker, "exact"
                )
            if qcec is None:
                raise RuntimeError(f"{record.id}: QCEC did not run on a pool-B output")
            return verdict(VerdictStatus(qcec["status"]), qcec["reason"], qcec["checker"], "qcec")
        finally:
            path.unlink(missing_ok=True)

    # -- metrics, provenance, audit, close -------------------------------------------------

    def metrics(self, task: Task, attempt: Attempt, verdict: Verdict) -> Metrics:
        if attempt.status != AttemptStatus.COMPLETED:
            return dict.fromkeys(METRIC_KEYS)
        counts = verdict.details.get("counts")
        if not isinstance(counts, dict):  # no analysis (no-output verdicts): nothing verified
            counts = {}
        return {
            "two_qubit_gates": counts.get("two_qubit_gates"),
            "depth": counts.get("depth"),
            "size": counts.get("size"),
            "compile_time_s": attempt.details.get("compile_time_s"),
            "active_qubits": verdict.details.get("active_qubits"),
            "physical_qubits": counts.get("physical_qubits"),
        }

    def provenance(self) -> dict[str, Any]:
        return {
            "adapter": f"{ADAPTER_NAME} {ADAPTER_VERSION}",
            "python": platform.python_version(),
            "qiskit": self.qiskit_version,
            "numpy": self.numpy_version,
            "mqt.qcec": qcec_check.qcec_version(),
            "mqt.core": metadata.version("mqt.core"),
            "qasmbench_repo": REPO_URL,
            "qasmbench_commit": self.options.commit,
            "split_sha256": self.split_sha256,
            "target_sha256": self.target_sha256,
            "qpy_version": str(QPY_VERSION),
            "exact_checker": self.exact_checker,
            "target_checker": TARGET_CHECK_ID,
        }

    def audit_split(self, dev: list[Task], held_out_ids: Sequence[str]) -> list[str]:
        """Split problems: shared ids, roles that contradict the split, and held-out circuits
        identical to a dev circuit (same OpenQASM 2 after removing final measurements)."""
        from qiskit import qasm2

        problems: list[str] = []
        dev_ids = [task.id for task in dev]
        held = list(held_out_ids)
        for task_id in sorted(set(dev_ids) & set(held)):
            problems.append(f"{task_id} is both a dev and a held-out task")
        for ids, role in ((dev_ids, "dev"), (held, "held_out")):
            for task_id in ids:
                recorded = self._rows.get(task_id, {}).get("role")
                if recorded != role:
                    problems.append(f"{task_id} is used as {role} but the split says {recorded!r}")
        dumps: dict[str, list[str]] = {}
        for task_id in dev_ids:
            try:
                self._load(task_id)
                text = qasm2.dumps(self._parsed[task_id])
            except Exception as exc:  # reported, not raised: the audit lists every problem
                problems.append(f"dev {task_id} cannot be compared: {type(exc).__name__}: {exc}")
                continue
            dumps.setdefault(text, []).append(task_id)
        for task_id in held:
            try:
                _, circuit = self._read_circuit(task_id)  # not cached: kept out of the search
                text = qasm2.dumps(circuit)
            except Exception as exc:
                problems.append(
                    f"held-out {task_id} cannot be compared: {type(exc).__name__}: {exc}"
                )
                continue
            if text in dumps:
                problems.append(
                    f"held-out {task_id} is the same circuit as dev {', '.join(dumps[text])} "
                    "(identical OpenQASM 2 after removing final measurements)"
                )
        return problems

    async def close(self) -> None:
        """Kill the worker processes and remove the scratch directory."""
        self._resources.release()

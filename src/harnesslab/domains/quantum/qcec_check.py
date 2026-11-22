"""Decision-diagram equivalence check with MQT QCEC: the decider for pool B, a cross-check on A.

The check is ``mqt.qcec.verify(original, compiled, run_alternating_checker=True,
run_construction_checker=False, run_simulation_checker=False, run_zx_checker=..., timeout=...)``
on the input circuit and the compiled circuit with its layout. It runs as a job in a worker
process (:class:`~harnesslab.domains.quantum.compile.WorkerPool`) whose process group is
killed after ``kill_after_s``: QCEC's own timeout is cooperative, and on medium/dnn_n16 it ran
for more than ten minutes past a 60 s timeout.

The hand-over, checked with deliberately broken circuits in ``tests/test_domain_quantum.py``:

* mqt.core reads the compiled circuit's initial layout and output permutation from its
  ``TranspileLayout``. It recognises ancillas by the layout's register names, which QPY does
  not keep, so the job marks every logical qubit from the input's qubit count upwards as
  ancillary itself: it starts in |0> and, not being garbage, must end in |0>.
* QCEC 3.12.0's ZX checker does not notice an ancilla left out of |0>: it reports a compiled
  circuit with an extra X on an ancilla as ``equivalent_up_to_global_phase``, and when it
  finishes before the alternating checker (which says ``not_equivalent``) its answer is the
  result. Seen on small/adder_n4 on the study's target and on the test circuits. The ZX checker
  therefore runs only when no ancilla is active, that is, when every operation acts on physical
  qubits that start with an input qubit; otherwise the alternating checker decides alone.

While QCEC runs, the worker's address space may grow by at most half of the machine's memory
and three quarters of what is available when the job starts (:func:`default_memory_budget`):
on medium/vqe_n24 (ten million gates once compiled) QCEC exhausted the machine before the kill.
Workers are also the kernel's first choice when memory runs out anyway (see ``compile``).

Outcomes: ``equivalent`` and ``equivalent_up_to_global_phase`` pass; ``not_equivalent`` fails;
anything else (``no_information``, ``probably_equivalent``, ``probably_not_equivalent``,
``equivalent_up_to_phase``, an exception inside QCEC, a killed or crashed process) is
unverifiable. A job that cannot even load its inputs raises, which stops the phase.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from harnesslab.domains.quantum.compile import (
    JobTimeout,
    WorkerDied,
    WorkerPool,
    cached_input,
    load_compiled,
)

PASS_RESULTS = frozenset({"equivalent", "equivalent_up_to_global_phase"})
FAIL_RESULTS = frozenset({"not_equivalent"})


def qcec_version() -> str:
    return metadata.version("mqt.qcec")


def checker_name(zx: bool) -> str:
    return f"mqt.qcec {qcec_version()} " + ("alternating+zx" if zx else "alternating")


def checker_options(zx: bool, timeout_s: float) -> dict[str, Any]:
    return {
        "run_alternating_checker": True,
        "run_construction_checker": False,
        "run_simulation_checker": False,
        "run_zx_checker": zx,
        "timeout": timeout_s,
    }


def half_the_memory() -> int | None:
    """Half of the machine's physical memory in bytes (None where it cannot be read)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2
    except (AttributeError, OSError, ValueError):
        return None


def _proc_kib(path: str, field: str) -> int | None:
    """A ``<field>: <n> kB`` line of a ``/proc`` file, in bytes (None where unreadable)."""
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                if line.startswith(f"{field}:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _address_space_in_use() -> int | None:
    """This process's virtual memory size in bytes (from ``/proc``; None elsewhere)."""
    return _proc_kib("/proc/self/status", "VmSize")


def default_memory_budget() -> int | None:
    """How much memory a QCEC job may add: half of the machine, and no more than three
    quarters of what the kernel reports available when the job starts."""
    available = _proc_kib("/proc/meminfo", "MemAvailable")
    limits = [half_the_memory(), None if available is None else available * 3 // 4]
    known = [limit for limit in limits if limit is not None]
    return min(known) if known else None


def _cap_address_space(budget: int | None) -> Callable[[], None]:
    """Let this process's address space grow by at most ``budget`` bytes; return what lifts it.

    The soft ``RLIMIT_AS`` is set to the current size plus ``budget``, so an allocation past
    it fails inside the job (``MemoryError``, or the worker aborts) instead of exhausting the
    machine. Measuring from the current size keeps a worker that still holds large caches
    from failing small checks. Without ``resource`` or ``/proc``, nothing changes.
    """
    try:
        import resource
    except ImportError:
        return lambda: None
    in_use = _address_space_in_use()
    if budget is None or in_use is None:
        return lambda: None
    limit = in_use + budget
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    if soft != resource.RLIM_INFINITY and soft <= limit:
        return lambda: None
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    return lambda: resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def qcec_job(
    source: str,
    sha256: str,
    name: str,
    compiled_path: str,
    compiled_sha256: str,
    zx: bool,
    timeout_s: float,
    memory_budget: int | None,
) -> dict[str, Any]:
    """Run QCEC on the input circuit and a compiled circuit (runs in a worker).

    Returns the equivalence criterion's name with QCEC's timings and per-checker results, or
    ``error`` when QCEC itself raises on this pair or needs more than ``memory_budget`` bytes
    (default: :func:`default_memory_budget`, taken when the job starts) beyond what the
    worker already holds. Loading the inputs is not guarded: a missing file or a hash
    mismatch raises.
    """
    from mqt import qcec
    from mqt.core import load as load_computation

    original = cached_input(source, sha256, name)
    compiled = load_compiled(Path(compiled_path), compiled_sha256)
    budget = default_memory_budget() if memory_budget is None else memory_budget
    started = time.perf_counter()
    first = second = results = None
    error: str | None = None
    restore = _cap_address_space(budget)
    try:
        first = load_computation(original)
        second = load_computation(compiled)
        for logical in range(original.num_qubits, second.num_qubits):
            second.set_circuit_qubit_ancillary(logical)
        results = qcec.verify(first, second, **checker_options(zx, timeout_s))
    except MemoryError:
        error = "MemoryError"
    except Exception as exc:  # QCEC could not handle this pair: recorded, never a pass
        error = f"{type(exc).__name__}: {exc}"
    finally:
        restore()
        del first, second
    duration = time.perf_counter() - started
    if error is not None or results is None:
        if error == "MemoryError" and budget is not None:
            error = f"MemoryError: QCEC needed more than {budget / 2**30:.1f} GiB more"
        return {"error": error or "QCEC returned no result", "qcec_duration_s": duration}
    summary = results.json()
    return {
        "equivalence": results.equivalence.name,
        "qcec_duration_s": duration,
        "check_time_s": summary.get("check_time"),
        "preprocessing_time_s": summary.get("preprocessing_time"),
        "checkers": [
            {
                "checker": entry.get("checker"),
                "equivalence": entry.get("equivalence"),
                "runtime_s": entry.get("runtime"),
            }
            for entry in summary.get("checkers", [])
        ],
    }


def interpret(outcome: dict[str, Any]) -> tuple[str, str]:
    """``(status, reason)`` of a :func:`qcec_job` result; status is pass, fail or unverifiable."""
    if "error" in outcome:
        return "unverifiable", f"QCEC could not check the pair: {outcome['error']}"
    equivalence = outcome["equivalence"]
    if equivalence in PASS_RESULTS:
        return "pass", f"QCEC: {equivalence}"
    if equivalence in FAIL_RESULTS:
        return "fail", f"QCEC: {equivalence}"
    return (
        "unverifiable",
        f"QCEC: {equivalence} (only equivalent or equivalent_up_to_global_phase count)",
    )


async def run_qcec(
    pool: WorkerPool,
    *,
    source: str,
    sha256: str,
    name: str,
    compiled_path: str,
    compiled_sha256: str,
    zx: bool,
    timeout_s: float,
    kill_after_s: float,
    memory_budget: int | None = None,
) -> dict[str, Any]:
    """Run :func:`qcec_job` in ``pool``, killing its worker after ``kill_after_s`` seconds.

    ``memory_budget`` bounds how far the worker's address space may grow while QCEC runs
    (default: :func:`default_memory_budget`; QCEC on medium/vqe_n24's ten million gates
    exhausted the machine).
    Returns ``status``, ``reason``, ``checker``, ``zx``, ``killed``, the wall time including
    loading (``duration_s``) and whatever QCEC reported.
    """
    started = time.perf_counter()
    base: dict[str, Any] = {"checker": checker_name(zx), "zx": zx, "killed": False}
    try:
        outcome = await pool.call(
            "qcec",
            source,
            sha256,
            name,
            compiled_path,
            compiled_sha256,
            zx,
            timeout_s,
            memory_budget,
            timeout_s=kill_after_s,
        )
    except JobTimeout:
        return base | {
            "status": "unverifiable",
            "reason": f"QCEC did not finish within {kill_after_s:g} s; its process was killed",
            "killed": True,
            "duration_s": time.perf_counter() - started,
        }
    except WorkerDied as exc:
        return base | {
            "status": "unverifiable",
            "reason": f"the QCEC process died: {exc}",
            "duration_s": time.perf_counter() - started,
        }
    status, reason = interpret(outcome)
    return (
        base
        | outcome
        | {
            "status": status,
            "reason": reason,
            "duration_s": time.perf_counter() - started,
        }
    )

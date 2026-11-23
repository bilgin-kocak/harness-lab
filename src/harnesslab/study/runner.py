"""Execute cells (task × configuration × repetition) through an adapter, and record them.

Rules the runner enforces for every adapter:

* A cell that already has a valid record in its phase is never run again.
* Before a cell starts, what it may spend is reserved in the budget and a reservation line is
  written to disk; a reservation that never gets a record (the process died) is charged in full.
  The reservation is the spec's per-attempt cap, or less when the adapter states a lower worst
  case for the configuration (``max_attempt_cost_usd``). When the budget refuses only because
  other cells still hold reservations, the cell waits for them; any other refusal stops further
  starts, and the refusal is reported.
* ``run`` raising an ordinary exception is part of the system under test: the cell is recorded as
  an ``error`` attempt with a ``fail`` verdict from the core (there is no output to verify), it is
  charged its whole reservation (it may have spent money it could not report), and the phase goes
  on.
* A broken harness stops the phase: ``run`` raising :class:`HarnessError` or returning something
  other than an :class:`Attempt`, an attempt for another cell, ``verify`` or ``metrics`` raising,
  an invalid or over-the-cap cost, or a record that cannot be stored. The cell is recorded with a
  ``harness_error`` (it has no valid outcome and may run again once the harness is fixed), the
  cells already running finish and are recorded, and the first problem is raised.
* So does an attempt with status ``unavailable`` (the system could not start at all): it is not
  verified, it is recorded with ``harness_error`` "unavailable: <reason>" and the cost it
  reported, and the cell runs again once the system is available; it is never a loss.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import shutil
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harnesslab.study.budget import Budget, Reservation
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapter,
    RunContext,
    Task,
    Verdict,
    VerdictStatus,
    VerifyContext,
)
from harnesslab.study.store import StudyStore, encode_line


class HarnessError(RuntimeError):
    """The verifier, the metrics or the adapter broke a rule; results after it cannot be trusted."""


@dataclass(frozen=True)
class Cell:
    task: Task
    config: Config
    repetition: int
    stage: str

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.task.id, self.config.config_hash, self.repetition)


@dataclass
class RunSummary:
    phase: str
    planned: int = 0
    already_done: int = 0
    ran: int = 0
    not_started: int = 0
    budget_stop: str | None = None
    errors: int = 0
    harness_errors: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    verdicts: dict[str, int] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cell_seed(study: str, phase: str, cell: Cell) -> int:
    text = f"{study}:{phase}:{cell.task.id}:{cell.config.config_hash}:{cell.repetition}"
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def _clean(value: Any) -> Any:
    """``value`` with every string made encodable as UTF-8 (a lone surrogate becomes ``\\ud83d``
    text) and non-finite floats as text: what a reduced record may still contain."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {_clean(str(k)): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _attempt_text(attempt: Attempt, limit: int = 4000) -> str:
    """The attempt as text, for a record that cannot hold it as JSON (without the payload, which
    is never stored)."""
    try:
        text = repr(attempt.model_dump(mode="python"))
    except Exception as exc:  # an object whose repr itself breaks
        text = f"<{attempt.status} attempt: not representable: {type(exc).__name__}>"
    return _clean(text[:limit])


# Fields of a record that the core writes itself (a reduced record keeps them).
_CORE_FIELDS = (
    "phase",
    "stage",
    "task_id",
    "config_hash",
    "repetition",
    "started_at",
    "finished_at",
    "reservation_id",
    "reserved_usd",
    "charged_usd",
    "cell_seconds",
)


def _storable(
    record: dict[str, Any],
    attempt: Attempt,
    verdict: Verdict | None,
    metrics: dict[str, Any],
    problem: str | None,
) -> tuple[dict[str, Any], str | None]:
    """The complete record and ``problem``, if the store can write it; else a reduced record (the
    core's fields, the attempt as text, no verdict, no metrics) and a harness problem naming the
    serialisation failure.

    Dumping the attempt and the verdict happens here too: output that pydantic cannot serialise
    (bytes that are not UTF-8, arbitrary objects) or that JSON allows but UTF-8 does not (a lone
    surrogate) must still leave a record, with its reservation id and charge, rather than none.
    """
    try:
        full = {
            **record,
            "attempt": attempt.model_dump(mode="json"),
            "verdict": verdict.model_dump(mode="json") if verdict is not None else None,
            "metrics": metrics,
            "harness_error": problem,
        }
        encode_line(full)  # exactly what the store will write
        return full, problem
    except Exception as exc:
        reason = _clean(
            f"the record cannot be stored (serialisation failed): {type(exc).__name__}: {exc}"
        )
    problem = _clean(problem) if problem is not None else reason
    reduced = {k: _clean(v) for k, v in record.items() if k not in ("harness_traceback", "attempt")}
    reduced.update(
        attempt={"unstorable": _attempt_text(attempt), "reason": reason},
        verdict=None,
        metrics={},
        harness_error=problem,
    )
    try:
        encode_line(reduced)
    except Exception:  # a core field the store cannot write either: keep only the essentials
        reduced = {k: _clean(record.get(k)) for k in _CORE_FIELDS}
        reduced.update(attempt={"unstorable": reason}, verdict=None, metrics={})
        reduced["harness_error"] = problem
    return reduced, problem


async def run_cells(
    adapter: DomainAdapter,
    store: StudyStore,
    *,
    study: str,
    phase: str,
    cells: Sequence[Cell],
    budget: Budget,
    parallelism: int,
    keep_artifacts: bool,
    extra: dict[str, Any] | None = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> RunSummary:
    done = store.completed(phase)
    summary = RunSummary(phase=phase, planned=len(cells))
    todo = [c for c in cells if c.key not in done]
    summary.already_done = len(cells) - len(todo)
    stop = asyncio.Event()
    lock = asyncio.Lock()
    settled = asyncio.Condition()
    sem = asyncio.Semaphore(max(1, parallelism))
    failure: list[HarnessError] = []
    worst_case = getattr(adapter, "max_attempt_cost_usd", None)

    async def halt(problem: HarnessError | None) -> None:
        if problem is not None:
            failure.append(problem)
        stop.set()
        async with settled:
            settled.notify_all()

    async def reserve(cell: Cell) -> Reservation | None:
        need = budget.need_for(worst_case(cell.config) if worst_case is not None else None)
        async with settled:
            while True:
                if stop.is_set():
                    return None
                res = budget.try_reserve(need)
                if isinstance(res, Reservation):
                    return res
                if res.retryable:
                    await settled.wait()
                    continue
                summary.budget_stop = summary.budget_stop or res.reason
                stop.set()
                settled.notify_all()
                return None

    async def one(cell: Cell) -> None:
        async with sem:
            if stop.is_set():
                summary.not_started += 1
                return
            reservation = await reserve(cell)
            if reservation is None:
                summary.not_started += 1
                return
            reservation_id = uuid.uuid4().hex
            started = _now()
            attempt_dir = store.artifacts_dir(
                phase, cell.task.id, cell.config.config_hash, cell.repetition
            )
            async with lock:
                store.reserve(
                    phase,
                    {
                        "id": reservation_id,
                        "stage": cell.stage,
                        "task_id": cell.task.id,
                        "config_hash": cell.config.config_hash,
                        "repetition": cell.repetition,
                        "usd": reservation.usd,
                        "started_at": started,
                    },
                )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            t0 = time.monotonic()
            ctx = RunContext(
                study=study,
                phase=phase,
                repetition=cell.repetition,
                attempt_dir=attempt_dir,
                keep_artifacts=keep_artifacts,
                cost_limit_usd=reservation.usd if budget.per_attempt_usd is not None else None,
                seed=cell_seed(study, phase, cell),
            )
            raised = False
            problem: str | None = None
            record: dict[str, Any] = {
                "phase": phase,
                "stage": cell.stage,
                "task_id": cell.task.id,
                "task_hash": cell.task.content_hash,
                "task_tags": list(cell.task.tags),
                "config_hash": cell.config.config_hash,
                "config_label": cell.config.label,
                "config": cell.config.values,
                "repetition": cell.repetition,
                "seed": ctx.seed,
                "started_at": started,
                "adapter": {"name": adapter.name, "version": adapter.version},
                "reservation_id": reservation_id,
                "reserved_usd": reservation.usd,
                **(extra or {}),
            }
            attempt: Attempt | None = None
            try:
                result = await adapter.run(cell.task, cell.config, ctx)
                if isinstance(result, Attempt):
                    attempt = result
                else:
                    problem = f"run() returned {type(result).__name__}, not an Attempt"
            except HarnessError as exc:
                problem = f"run() reported a harness error: {exc}"
                record["harness_traceback"] = traceback.format_exc()[-4000:]
            except Exception as exc:  # the system under test failed: a recorded result
                raised = True
                attempt = Attempt(
                    task_id=cell.task.id,
                    config_hash=cell.config.config_hash,
                    repetition=cell.repetition,
                    status=AttemptStatus.ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                    details={"traceback": traceback.format_exc()[-4000:]},
                )
            if attempt is None:  # a harness problem inside run(): no attempt to keep
                raised = True  # nothing reported: charge the whole reservation
                attempt = Attempt(
                    task_id=cell.task.id,
                    config_hash=cell.config.config_hash,
                    repetition=cell.repetition,
                    status=AttemptStatus.ERROR,
                    error=problem,
                )
            run_seconds = time.monotonic() - t0
            if attempt.wall_time_s is None:
                attempt.wall_time_s = run_seconds
            if (
                problem is None
                and (
                    attempt.task_id,
                    attempt.config_hash,
                    attempt.repetition,
                )
                != cell.key
            ):
                problem = f"adapter returned an attempt for {attempt.task_id}/{attempt.repetition}"
            if problem is None and not raised and attempt.status == AttemptStatus.UNAVAILABLE:
                # The system could not start at all (a missing CLI, an outage): there is nothing
                # to verify and no outcome to count, so the cell is left without one (it runs
                # again) and the phase stops like for any other harness problem.
                problem = f"unavailable: {attempt.error or 'the system could not start'}"
            verdict: Verdict | None = None
            metrics: dict[str, Any] = {}
            if problem is None and raised:
                # Nothing was produced, so there is nothing to verify: the core records the failure
                # itself rather than handing the adapter's verifier an empty attempt.
                verdict = Verdict(
                    status=VerdictStatus.FAIL,
                    checker="harnesslab.study runner",
                    reason=f"run() raised: {attempt.error}",
                )
            elif problem is None:
                try:
                    verdict = await adapter.verify(
                        cell.task,
                        attempt,
                        VerifyContext(
                            study=study,
                            phase=phase,
                            attempt_dir=attempt_dir,
                            keep_artifacts=keep_artifacts,
                        ),
                    )
                    if not isinstance(verdict, Verdict):
                        raise TypeError(
                            f"verify() returned {type(verdict).__name__}, not a Verdict"
                        )
                    metrics = dict(adapter.metrics(cell.task, attempt, verdict))
                    bad = [
                        k
                        for k, v in metrics.items()
                        if isinstance(v, float) and not math.isfinite(v)
                    ]
                    if bad:
                        raise ValueError(f"non-finite metric value(s): {', '.join(sorted(bad))}")
                except Exception as exc:
                    problem = f"verify/metrics failed: {type(exc).__name__}: {exc}"
                    record["harness_traceback"] = traceback.format_exc()[-4000:]
            cost = attempt.usage.cost_usd
            cost_ok = isinstance(cost, int | float) and math.isfinite(cost) and cost >= 0
            if problem is None and not raised:
                if not cost_ok:
                    problem = f"attempt reported an invalid cost {cost!r}"
                elif ctx.cost_limit_usd is not None and cost > ctx.cost_limit_usd + 1e-9:
                    problem = f"attempt spent ${cost:.6f}, above its cap ${ctx.cost_limit_usd:.6f}"
            elapsed = time.monotonic() - t0
            # A run that raised may have spent money it could not report: charge its whole
            # reservation (the most it was allowed to spend), so the budget never undercounts.
            charged = reservation.usd if raised or not cost_ok else float(cost)
            budget.settle(reservation, charged, elapsed)
            record.update(finished_at=_now(), cell_seconds=round(elapsed, 6), charged_usd=charged)
            record, problem = _storable(
                record,
                attempt,
                verdict if problem is None else None,
                metrics if problem is None else {},
                problem,
            )
            async with lock:
                store.append(phase, record)
                summary.ran += 1
                summary.cost_usd += charged
                summary.seconds += elapsed
                if attempt.status == AttemptStatus.ERROR:
                    summary.errors += 1
                if problem is not None:
                    summary.harness_errors += 1
                key = (
                    "harness_error"
                    if problem is not None
                    else (verdict.status.value if verdict else "none")
                )
                summary.verdicts[key] = summary.verdicts.get(key, 0) + 1
            async with settled:
                settled.notify_all()
            if not keep_artifacts and attempt_dir.exists() and not any(attempt_dir.iterdir()):
                shutil.rmtree(attempt_dir, ignore_errors=True)
            if problem is not None:
                await halt(HarnessError(f"{cell.task.id} / {cell.config.label}: {problem}"))
                return
            if summary.ran % 25 == 0:
                log(f"{phase}: {summary.ran + summary.already_done}/{summary.planned} cells")

    async def guarded(cell: Cell) -> None:
        try:
            await one(cell)
        except Exception as exc:  # the harness itself failed (e.g. the disk): stop, keep the rest
            await halt(
                HarnessError(
                    f"{cell.task.id} / {cell.config.label}: harness failure: "
                    f"{type(exc).__name__}: {exc}"
                )
            )

    tasks = [asyncio.create_task(guarded(c)) for c in todo]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    if failure:
        raise failure[0]
    return summary

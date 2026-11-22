"""Execute cells (task × configuration × repetition) through an adapter, and record them.

Rules the runner enforces for every adapter:

* A cell that already has a record in its phase is never run again.
* Before a cell starts, its cost cap is reserved in the budget; when the budget refuses, no
  further cell starts and the refusal is reported.
* ``run`` failing is part of the system under test: the cell is recorded as an ``error`` attempt
  with a ``fail`` verdict from the core (there is no output to verify), it is charged its whole
  reservation (it may have spent money it could not report), and the phase goes on. ``verify`` or ``metrics`` failing, or an attempt spending more than its
  cap, is a broken harness: the cell is recorded and the phase stops with an exception.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harnesslab.study.budget import Budget
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
from harnesslab.study.store import StudyStore


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
    cost_usd: float = 0.0
    seconds: float = 0.0
    verdicts: dict[str, int] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cell_seed(study: str, phase: str, cell: Cell) -> int:
    text = f"{study}:{phase}:{cell.task.id}:{cell.config.config_hash}:{cell.repetition}"
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


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
    sem = asyncio.Semaphore(max(1, parallelism))
    failure: list[BaseException] = []

    async def one(cell: Cell) -> None:
        async with sem:
            if stop.is_set():
                summary.not_started += 1
                return
            reservation = budget.try_reserve()
            if isinstance(reservation, str):
                summary.budget_stop = summary.budget_stop or reservation
                summary.not_started += 1
                stop.set()
                return
            attempt_dir = store.artifacts_dir(
                phase, cell.task.id, cell.config.config_hash, cell.repetition
            )
            attempt_dir.mkdir(parents=True, exist_ok=True)
            started = _now()
            t0 = time.monotonic()
            ctx = RunContext(
                study=study,
                phase=phase,
                repetition=cell.repetition,
                attempt_dir=attempt_dir,
                keep_artifacts=keep_artifacts,
                cost_limit_usd=budget.per_attempt_usd,
                seed=cell_seed(study, phase, cell),
            )
            raised = False
            try:
                attempt = await adapter.run(cell.task, cell.config, ctx)
                if not isinstance(attempt, Attempt):
                    raise HarnessError(f"run() returned {type(attempt).__name__}, not an Attempt")
            except HarnessError:
                raise
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
            run_seconds = time.monotonic() - t0
            if attempt.wall_time_s is None:
                attempt.wall_time_s = run_seconds
            record: dict[str, Any] = {
                "phase": phase,
                "stage": cell.stage,
                "task_id": cell.task.id,
                "task_hash": cell.task.content_hash,
                "config_hash": cell.config.config_hash,
                "config_label": cell.config.label,
                "config": cell.config.values,
                "repetition": cell.repetition,
                "seed": ctx.seed,
                "started_at": started,
                "adapter": {"name": adapter.name, "version": adapter.version},
                "reserved_usd": reservation.usd,
                **(extra or {}),
            }
            problem: str | None = None
            if (attempt.task_id, attempt.config_hash, attempt.repetition) != cell.key:
                problem = f"adapter returned an attempt for {attempt.task_id}/{attempt.repetition}"
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
                except Exception as exc:
                    problem = f"verify/metrics failed: {type(exc).__name__}: {exc}"
                    record["harness_traceback"] = traceback.format_exc()[-4000:]
            cost = attempt.usage.cost_usd
            if (
                problem is None
                and ctx.cost_limit_usd is not None
                and cost > ctx.cost_limit_usd + 1e-9
            ):
                problem = f"attempt spent ${cost:.6f}, above its cap ${ctx.cost_limit_usd:.6f}"
            elapsed = time.monotonic() - t0
            # A run that raised may have spent money it could not report: charge its whole
            # reservation (the most it was allowed to spend), so the budget never undercounts.
            charged = reservation.usd if raised else cost
            budget.settle(reservation, charged, elapsed)
            record.update(
                finished_at=_now(),
                cell_seconds=round(elapsed, 6),
                charged_usd=charged,
                attempt=attempt.model_dump(mode="json"),
                verdict=verdict.model_dump(mode="json") if verdict else None,
                metrics=metrics,
                harness_error=problem,
            )
            async with lock:
                store.append(phase, record)
                summary.ran += 1
                summary.cost_usd += charged
                summary.seconds += elapsed
                if attempt.status == AttemptStatus.ERROR:
                    summary.errors += 1
                key = verdict.status.value if verdict else "none"
                summary.verdicts[key] = summary.verdicts.get(key, 0) + 1
            if not keep_artifacts and attempt_dir.exists() and not any(attempt_dir.iterdir()):
                shutil.rmtree(attempt_dir, ignore_errors=True)
            if problem is not None:
                stop.set()
                failure.append(HarnessError(f"{cell.task.id} / {cell.config.label}: {problem}"))
                return
            if summary.ran % 25 == 0:
                log(f"{phase}: {summary.ran + summary.already_done}/{summary.planned} cells")

    tasks = [asyncio.create_task(one(c)) for c in todo]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    if failure:
        raise failure[0]
    return summary

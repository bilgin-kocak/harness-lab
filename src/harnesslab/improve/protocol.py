"""The improvement protocol: baseline, then rounds of agent work against a measured objective.

For an improvement task the experiment service hands the agent step to :func:`run_improvement`:

1. measure the baseline: the untouched repository must pass the correctness gate (the task's
   verification command with its hidden files) and its objective is measured;
2. for each round, run the harness with a prompt that reports the baseline, the best so far and
   the history of earlier rounds; an optional in-loop evaluator lets it measure the objective a
   limited number of times;
3. after each round, evaluate a *copy* of the worktree (gate, then objective), so hidden tests
   and objective files never appear in the agent's own worktree;
4. a round that beats the best becomes the new best checkpoint; with ``keep_best`` any other round
   is reverted to the best checkpoint before the next one.

The service then runs its usual final capture and verification on the resulting worktree; the
run passes when that final gate passes and the final value beats the baseline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import statistics
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import RunnerConfig, RunnerResult, RunStatus, TaskSpec, UsageTotals
from harnesslab.execution.sandbox import ExecutionSandbox, SandboxContext
from harnesslab.improve.checkpoint import exclude_in_git, restore, snapshot
from harnesslab.improve.evaluator import EVAL_DIR, count_calls, install_evaluator, reset_calls
from harnesslab.improve.objective import (
    improvement_ratio,
    improvement_score,
    is_better,
    parse_value,
    progress,
)
from harnesslab.runners.base import HarnessRunner
from harnesslab.verification.command import CommandVerifier


class ImproveBaselineError(RuntimeError):
    """The untouched repository fails the gate or cannot be measured: the task is broken."""


class Evaluation(BaseModel):
    gate_passed: bool | None
    value: float | None = None
    detail: str | None = None  # internal only; never shown to the agent


class RoundRecord(BaseModel):
    round: int
    status: str
    value: float | None = None
    gate_passed: bool | None = None
    best: float | None = None  # best so far after this round
    accepted: bool = False
    reverted: bool = False
    evaluator_calls: int = 0
    note: str | None = None


class ImproveResult(BaseModel):
    direction: str
    unit: str | None = None
    target: float | None = None
    keep_best: bool = True
    rounds_planned: int
    evaluator_budget: int
    baseline: float
    best: float
    best_round: int = 0
    final: float | None = None
    ratio: float | None = None
    score: float = 0.0
    progress: float | None = None
    improved: bool = False
    evaluator_calls: int = 0
    rounds: list[RoundRecord] = Field(default_factory=list)

    @property
    def curve(self) -> list[float | None]:
        return [self.baseline] + [r.best for r in self.rounds]


Invoke = Callable[[HarnessRunner, TaskSpec, RunnerConfig], Awaitable[RunnerResult]]


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def build_round_prompt(
    task: TaskSpec,
    round_no: int,
    rounds: int,
    *,
    baseline: float,
    best: float,
    best_round: int,
    history: list[RoundRecord],
    budget: int,
) -> str:
    spec = task.improve
    assert spec is not None
    objective = spec.objective
    better = "lower" if objective.direction == "minimize" else "higher"
    unit = f" {objective.unit}" if objective.unit else ""
    lines = [
        task.prompt.rstrip(),
        "",
        f"## Round {round_no} of {rounds}: improve the objective",
        "",
        "This repository already works. In this round, make the measured objective "
        f"{better} while every correctness check keeps passing.",
        "",
        f"- Objective: {objective.unit or 'the measured value'}, {better} is better.",
        f"- Baseline (the untouched repository): {_fmt(baseline)}{unit}",
        f"- Best so far: {_fmt(best)}{unit} "
        + (f"(round {best_round})" if best_round else "(the baseline)"),
    ]
    if objective.target is not None:
        lines.append(f"- Reference value to aim for: {_fmt(objective.target)}{unit}")
    if history:
        lines += ["", "Previous rounds:"]
        for record in history:
            lines.append(f"- Round {record.round}: {_describe(record, unit, spec.keep_best)}")
    lines.append("")
    if budget > 0:
        lines.append(
            "You can measure the objective of your current working tree by running "
            f"`python {EVAL_DIR}/evaluate.py`. It runs the same measurement Harness Lab uses, "
            f"without the correctness checks, and you may call it at most {budget} time(s) in "
            "this round."
        )
    else:
        lines.append("You cannot measure the objective directly in this round.")
    lines.append("")
    if spec.keep_best:
        lines.append(
            "After this round Harness Lab measures the objective and runs the correctness checks, "
            "including hidden ones. A round that fails a check or does not beat the best so far "
            "is reverted to the best version before the next round."
        )
    else:
        lines.append(
            "After this round Harness Lab measures the objective and runs the correctness checks, "
            "including hidden ones. Your changes carry over to the next round whatever the result."
        )
    return "\n".join(lines) + "\n"


def _describe(record: RoundRecord, unit: str, keep_best: bool) -> str:
    if record.gate_passed is None and record.value is None:
        return f"the agent did not run ({record.status})."
    if record.gate_passed is False:
        tail = "reverted to the best version." if record.reverted else "changes kept."
        return f"correctness checks failed; {tail}"
    if record.value is None:
        tail = "reverted to the best version." if record.reverted else "changes kept."
        return f"the objective could not be measured; {tail}"
    if record.accepted:
        return f"{_fmt(record.value)}{unit}, the new best."
    if record.reverted:
        return (
            f"{_fmt(record.value)}{unit}, not better than the best; reverted to the best version."
        )
    return f"{_fmt(record.value)}{unit}, not better than the best."


def _fresh_copy(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, symlinks=True, ignore=shutil.ignore_patterns(".git", EVAL_DIR))


def _inject(task: TaskSpec, workdir: Path) -> None:
    assert task.improve is not None
    root = workdir.resolve()
    for item in task.improve.objective.inject:
        source = task.resolve(item.source)
        dest = (workdir / item.dest).resolve()
        if dest != root and root not in dest.parents:
            raise ValueError(f"objective inject dest escapes the worktree: {item.dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(source, dest)


async def evaluate_state(
    task: TaskSpec, ctx: SandboxContext, sandbox: ExecutionSandbox, verifier: CommandVerifier
) -> Evaluation:
    """Gate, then objective, on a scratch copy of the agent's worktree."""
    spec = task.improve
    assert spec is not None
    eval_dir = ctx.workdir.parent / f"{ctx.run_id}.eval"
    await asyncio.to_thread(_fresh_copy, ctx.workdir, eval_dir)
    try:
        eval_ctx = dataclasses.replace(ctx, workdir=eval_dir)
        changes = await sandbox.capture_changes(ctx)
        verdict = await verifier.verify(task, sandbox, eval_ctx, changes)
        if not verdict.passed:
            detail = verdict.skipped_reason or f"verification exit code {verdict.exit_code}"
            return Evaluation(gate_passed=verdict.passed, detail=detail)
        await asyncio.to_thread(_inject, task, eval_dir)
        values: list[float] = []
        for _ in range(spec.objective.repeats):
            proc = await sandbox.run_command(
                eval_ctx,
                spec.objective.command,
                timeout=spec.objective.timeout_seconds,
                include_auth=False,
            )
            value = parse_value(proc.stdout) if proc.ok else None
            if value is None:
                reason = proc.error or f"exit {proc.exit_code}, timed_out={proc.timed_out}"
                return Evaluation(gate_passed=True, detail=f"objective not measured: {reason}")
            values.append(value)
        return Evaluation(gate_passed=True, value=statistics.median(values))
    finally:
        await asyncio.to_thread(shutil.rmtree, eval_dir, True)


def merge_runner_results(results: list[RunnerResult]) -> RunnerResult:
    """One result for all rounds: usage, cost and counts summed; status and messages from the last."""
    if not results:
        return RunnerResult(status=RunStatus.COMPLETED)
    usage = UsageTotals()
    by_model: dict[str, UsageTotals] = {}
    for result in results:
        usage = usage.add(result.usage)
        for model, part in result.usage_by_model.items():
            by_model[model] = by_model.get(model, UsageTotals()).add(part)

    def total(attr: str) -> Any:
        values = [getattr(r, attr) for r in results if getattr(r, attr) is not None]
        return sum(values) if values else None

    def last(attr: str) -> Any:
        return next((getattr(r, attr) for r in reversed(results) if getattr(r, attr)), None)

    final = results[-1]
    status = (
        RunStatus.UNAVAILABLE
        if any(r.status == RunStatus.UNAVAILABLE for r in results)
        else final.status
    )
    metadata = dict(final.metadata)
    metadata["improve_rounds"] = [
        {"round": i, "status": r.status.value, "error": r.error} for i, r in enumerate(results, 1)
    ]
    return RunnerResult(
        status=status,
        exit_code=final.exit_code,
        final_message=last("final_message"),
        usage=usage,
        usage_by_model=by_model,
        reported_cost_usd=total("reported_cost_usd"),
        provider_session_id=last("provider_session_id"),
        model_resolved=last("model_resolved"),
        cli_version=last("cli_version"),
        num_turns=total("num_turns"),
        llm_calls=total("llm_calls"),
        permission_denials=sum(r.permission_denials for r in results),
        error=final.error,
        metadata=metadata,
    )


def _trace_evaluator_calls(emitter: EventEmitter, start: int) -> int:
    needle = f"{EVAL_DIR}/evaluate.py"
    return sum(
        1
        for event in emitter.events[start:]
        if event.kind == EventKind.COMMAND_STARTED
        and needle in str(event.payload.get("command", ""))
    )


async def run_improvement(
    *,
    task: TaskSpec,
    config: RunnerConfig,
    ctx: SandboxContext,
    sandbox: ExecutionSandbox,
    verifier: CommandVerifier,
    emitter: EventEmitter,
    make_runner: Callable[[Path], HarnessRunner],
    invoke: Invoke,
    artifacts_dir: Path,
) -> tuple[RunnerResult, ImproveResult]:
    spec = task.improve
    assert spec is not None
    objective = spec.objective
    rounds = max(1, int(config.get("improve_rounds", spec.rounds)))
    budget = max(0, int(config.get("improve_eval_budget", spec.evaluator.budget)))
    workdir = ctx.workdir

    await asyncio.to_thread(exclude_in_git, workdir, f"{EVAL_DIR}/")
    if budget > 0:
        install_evaluator(
            workdir,
            command=objective.command,
            inject=[(task.resolve(item.source), item.dest) for item in objective.inject],
            budget=budget,
            repeats=objective.repeats,
            timeout=objective.timeout_seconds,
            unit=objective.unit,
            direction=objective.direction,
        )

    measured = await evaluate_state(task, ctx, sandbox, verifier)
    emitter.emit(
        EventKind.SYSTEM,
        name="improve_baseline",
        payload={"value": measured.value, "gate_passed": measured.gate_passed},
    )
    if not measured.gate_passed or measured.value is None:
        raise ImproveBaselineError(
            "improvement baseline is not usable: the untouched repository "
            + (
                "fails the correctness gate"
                if not measured.gate_passed
                else "could not be measured"
            )
            + (f" ({measured.detail})" if measured.detail else "")
        )

    baseline = measured.value
    best, best_round, best_commit = baseline, 0, ctx.base_commit
    last_value: float | None = baseline
    records: list[RoundRecord] = []
    results: list[RunnerResult] = []
    total_calls = 0
    for round_no in range(1, rounds + 1):
        if budget > 0:
            reset_calls(workdir)
        prompt = build_round_prompt(
            task,
            round_no,
            rounds,
            baseline=baseline,
            best=best,
            best_round=best_round,
            history=records,
            budget=budget,
        )
        round_dir = artifacts_dir / f"round-{round_no}"
        round_dir.mkdir(parents=True, exist_ok=True)
        (round_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        emitter.emit(
            EventKind.SYSTEM,
            name="improve_round_started",
            payload={"round": round_no, "rounds": rounds, "best": best, "baseline": baseline},
        )
        start = len(emitter.events)
        result = await invoke(
            make_runner(round_dir),
            task.model_copy(update={"prompt": prompt}),
            config.model_copy(update={"improve_round": round_no}),
        )
        results.append(result)
        calls = max(
            count_calls(workdir) if budget > 0 else 0, _trace_evaluator_calls(emitter, start)
        )
        total_calls += calls
        if result.status == RunStatus.UNAVAILABLE:
            records.append(
                RoundRecord(
                    round=round_no,
                    status=result.status.value,
                    best=best,
                    evaluator_calls=calls,
                    note=result.error,
                )
            )
            break
        evaluation = await evaluate_state(task, ctx, sandbox, verifier)
        value = evaluation.value
        accepted = bool(
            evaluation.gate_passed
            and value is not None
            and is_better(value, best, objective.direction)
        )
        reverted = False
        if accepted:
            best, best_round = value, round_no
            best_commit = await asyncio.to_thread(
                snapshot, workdir, ctx.base_commit, f"harnesslab improve round {round_no}"
            )
        elif spec.keep_best:
            changed = not evaluation.gate_passed or value is None or value != best
            await asyncio.to_thread(restore, workdir, best_commit)
            reverted = changed
        last_value = value if evaluation.gate_passed else None
        record = RoundRecord(
            round=round_no,
            status=result.status.value,
            value=value,
            gate_passed=evaluation.gate_passed,
            best=best,
            accepted=accepted,
            reverted=reverted,
            evaluator_calls=calls,
        )
        records.append(record)
        emitter.emit(EventKind.SYSTEM, name="improve_round", payload=record.model_dump(mode="json"))

    final = best if spec.keep_best else last_value
    ratio = improvement_ratio(baseline, final, objective.direction)
    outcome = ImproveResult(
        direction=objective.direction,
        unit=objective.unit,
        target=objective.target,
        keep_best=spec.keep_best,
        rounds_planned=rounds,
        evaluator_budget=budget,
        baseline=baseline,
        best=best,
        best_round=best_round,
        final=final,
        ratio=ratio,
        score=improvement_score(ratio) if final is not None else 0.0,
        progress=progress(baseline, final, objective.target),
        improved=final is not None
        and is_better(final, baseline, objective.direction, spec.min_improvement),
        evaluator_calls=total_calls,
        rounds=records,
    )
    (artifacts_dir / "improve.json").write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
    return merge_runner_results(results), outcome

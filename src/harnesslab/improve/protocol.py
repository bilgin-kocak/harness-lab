"""The improvement protocol: baseline, then rounds of agent work against a measured objective.

For an improvement task the experiment service hands the agent step to :func:`run_improvement`:

1. measure the baseline: the untouched repository must pass the correctness gate (the task's
   verification command with its hidden files) and its objective is measured;
2. for each round, run the harness with a prompt that reports the baseline, the best so far and
   the history of earlier rounds; an optional in-loop evaluator lets it measure the objective a
   limited number of times;
3. after each round, evaluate a *copy* of the worktree (gate, then objective), so hidden tests
   never appear in the agent's own worktree (and the objective's files only do, under
   ``.harnesslab_eval``, when the agent has an in-loop evaluator);
4. a round that beats the best becomes the new best checkpoint; with ``keep_best`` any other round
   is reverted to the best checkpoint before the next one. A round whose state cannot be evaluated
   at all counts as failed. If a checkpoint or a revert itself fails, the protocol stops there and
   keeps what it has: every round's usage, the history, and the worktree's actual state.

Each round runs in a fresh agent session by default (``improve_session: fresh``). With
``improve_session: resume`` the first round's session is kept and later rounds continue it with a
short delta prompt (:func:`build_resume_prompt`) instead of the full one, so the two modes can be
compared on the same task.

The service then runs its usual final capture and verification on the resulting worktree; the
run passes when that final gate passes and the final value beats the baseline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import shutil
import stat
import statistics
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.core.events import EventEmitter, EventKind
from harnesslab.core.models import RunnerConfig, RunnerResult, RunStatus, TaskSpec, UsageTotals
from harnesslab.execution.sandbox import ExecutionSandbox, SandboxContext
from harnesslab.improve.checkpoint import anchor, drop_anchor, exclude_in_git, restore, snapshot
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

SESSION_MODES = ("fresh", "resume")


class ImproveSetupError(RuntimeError):
    """The improvement protocol cannot start: a bad option, a runner or a broken task."""


class ImproveBaselineError(ImproveSetupError):
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
    session: str = "fresh"  # fresh: a new agent session per round; resume: one session for all
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
        *_standing(task, baseline=baseline, best=best, best_round=best_round),
    ]
    if history:
        lines += ["", "Previous rounds:"]
        for record in history:
            lines.append(f"- Round {record.round}: {_describe(record, unit, spec.keep_best)}")
    lines.append("")
    if budget > 0:
        lines.append(_evaluator_text(budget))
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


def build_resume_prompt(
    task: TaskSpec,
    round_no: int,
    rounds: int,
    *,
    baseline: float,
    best: float,
    best_round: int,
    unseen: list[RoundRecord],
    budget: int,
) -> str:
    """The short prompt for a round that resumes the agent's session.

    The session already holds the task and the earlier rounds' prompts, so this reports only what
    the agent has not seen: the results of the rounds since its last turn (``unseen``, usually
    just the previous round), the standing, and a revert, which makes its memory of its own edits
    stale.
    """
    spec = task.improve
    assert spec is not None
    better = "lower" if spec.objective.direction == "minimize" else "higher"
    unit = f" {spec.objective.unit}" if spec.objective.unit else ""
    lines = [f"## Round {round_no} of {rounds}: keep improving the objective", ""]
    if unseen:
        lines.append("Previous round:" if len(unseen) == 1 else "Rounds since your last prompt:")
        for record in unseen:
            lines.append(f"- Round {record.round}: {_describe(record, unit, spec.keep_best)}")
        lines.append("")
    lines += [*_standing(task, baseline=baseline, best=best, best_round=best_round), ""]
    if unseen and unseen[-1].reverted:
        version = f"round {best_round}" if best_round else "the untouched repository"
        lines += [
            f"Harness Lab reverted the files to the best version ({version}), so the working "
            "tree no longer holds the edits you made after it. Read the files again before you "
            "change them.",
            "",
        ]
    if budget > 0:
        lines += [_evaluator_text(budget), ""]
    lines.append(
        f"Keep improving: make the objective {better} while every correctness check keeps "
        "passing. Harness Lab measures and checks this round the same way as the earlier ones."
    )
    return "\n".join(lines) + "\n"


def _standing(task: TaskSpec, *, baseline: float, best: float, best_round: int) -> list[str]:
    assert task.improve is not None
    objective = task.improve.objective
    unit = f" {objective.unit}" if objective.unit else ""
    lines = [
        f"- Baseline (the untouched repository): {_fmt(baseline)}{unit}",
        f"- Best so far: {_fmt(best)}{unit} "
        + (f"(round {best_round})" if best_round else "(the baseline)"),
    ]
    if objective.target is not None:
        lines.append(f"- Reference value to aim for: {_fmt(objective.target)}{unit}")
    return lines


def _evaluator_text(budget: int) -> str:
    return (
        "You can measure the objective of your current working tree by running "
        f"`python {EVAL_DIR}/evaluate.py`. It runs the same measurement Harness Lab uses, "
        f"without the correctness checks, and you may call it at most {budget} time(s) in "
        "this round."
    )


def _describe(record: RoundRecord, unit: str, keep_best: bool) -> str:
    tail = "reverted to the best version." if record.reverted else "changes kept."
    if record.status == RunStatus.UNAVAILABLE.value:
        return "the agent did not run (the harness was unavailable)."
    if record.gate_passed is None:
        return f"this round could not be evaluated; {tail}"
    if record.gate_passed is False:
        return f"correctness checks failed; {tail}"
    if record.value is None:
        return f"the objective could not be measured; {tail}"
    if record.accepted:
        return f"{_fmt(record.value)}{unit}, the new best."
    if record.reverted:
        return (
            f"{_fmt(record.value)}{unit}, not better than the best; reverted to the best version."
        )
    return f"{_fmt(record.value)}{unit}, not better than the best."


def _copy_regular(source: str, dest: str) -> None:
    # Like git, skip named pipes, sockets and devices: they are not part of the code.
    if stat.S_ISREG(os.lstat(source).st_mode):
        shutil.copy2(source, dest)


def _fresh_copy(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        source,
        dest,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", EVAL_DIR),
        copy_function=_copy_regular,
    )


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


async def _evaluate(
    task: TaskSpec, ctx: SandboxContext, sandbox: ExecutionSandbox, verifier: CommandVerifier
) -> Evaluation:
    """:func:`evaluate_state`, with a state it cannot handle (say, a named pipe) as unevaluated."""
    try:
        return await evaluate_state(task, ctx, sandbox, verifier)
    except Exception as exc:
        return Evaluation(
            gate_passed=None, detail=f"evaluation failed: {type(exc).__name__}: {exc}"
        )


def per_round_totals(result: RunnerResult, previous: RunnerResult | None) -> RunnerResult:
    """``result``'s own share of a resumed session's running totals.

    A resumed Claude Code session reports ``total_cost_usd`` and per-model usage as totals for the
    whole session so far (its per-invocation ``usage`` is reported separately and stays as it
    is). Subtracting what the session reported last time leaves this invocation's share, so
    adding up the rounds counts every token and every cent once.
    """
    if previous is None:
        return result
    cost = result.reported_cost_usd
    if cost is not None and previous.reported_cost_usd is not None:
        cost = max(0.0, cost - previous.reported_cost_usd)
    by_model = {
        model: _minus(usage, previous.usage_by_model.get(model))
        for model, usage in result.usage_by_model.items()
    }
    return result.model_copy(update={"reported_cost_usd": cost, "usage_by_model": by_model})


def _minus(total: UsageTotals, earlier: UsageTotals | None) -> UsageTotals:
    if earlier is None:
        return total
    return UsageTotals(
        **{
            name: max(0, getattr(total, name) - getattr(earlier, name))
            for name in UsageTotals.model_fields
        }
    )


def merge_runner_results(
    results: list[RunnerResult], redact: Callable[[str], str] | None = None
) -> RunnerResult:
    """One result for all rounds: usage, cost and counts summed; status and messages from the last."""
    redact = redact or (lambda text: text)
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
        {
            "round": i,
            "status": r.status.value,
            "error": redact(r.error) if r.error else None,
            "session_id": r.provider_session_id,
        }
        for i, r in enumerate(results, 1)
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


# Running the evaluator (directly or through an interpreter), not reading it.
_EVALUATOR_RUN = re.compile(
    r"(?:^|[;&|(]\s*)(?:\S*python[\d.]*\s+)?(?:\./)?" + re.escape(EVAL_DIR) + r"/evaluate\.py\b"
)


def _trace_evaluator_calls(emitter: EventEmitter, start: int) -> int:
    return sum(
        1
        for event in emitter.events[start:]
        if event.kind == EventKind.COMMAND_STARTED
        and _EVALUATOR_RUN.search(str(event.payload.get("command", "")))
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
    redact: Callable[[str], str] | None = None,
) -> tuple[RunnerResult, ImproveResult]:
    spec = task.improve
    assert spec is not None
    objective = spec.objective
    rounds = max(1, int(config.get("improve_rounds", spec.rounds)))
    budget = max(0, int(config.get("improve_eval_budget", spec.evaluator.budget)))
    session = config.get("improve_session", "fresh")
    if session not in SESSION_MODES:
        raise ImproveSetupError(
            f"invalid improve_session {session!r}; expected one of: {', '.join(SESSION_MODES)}"
        )
    resume = session == "resume"
    if resume:
        problem = make_runner(artifacts_dir).resume_error(config)
        if problem:
            raise ImproveSetupError(f"improve_session: resume is not available: {problem}")
    workdir = ctx.workdir
    redact = redact or (lambda text: text)

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

    measured = await _evaluate(task, ctx, sandbox, verifier)
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
    if baseline < 0 or (objective.direction == "minimize" and baseline == 0):
        raise ImproveBaselineError(
            f"improvement baseline is not usable: the objective measures {baseline:g}, which "
            + ("is negative" if baseline < 0 else "is already 0 and cannot be lowered")
            + "; improvement is scored as a ratio, so the objective must be non-negative"
        )

    anchor_ref = f"refs/harnesslab/checkpoints/{ctx.run_id}"
    best, best_round, best_commit = baseline, 0, ctx.base_commit
    current: float | None = baseline  # the measured value of the worktree's present state
    records: list[RoundRecord] = []
    results: list[RunnerResult] = []
    total_calls = 0
    stopped: str | None = None
    # Resume mode: the most recent session id any round reported, and the round that reported it.
    session_id: str | None = None
    session_round = 0
    lost_session = False  # the last resume reported no session id, so it failed
    # The totals each session last reported, for runners whose resumed sessions report running
    # totals (see per_round_totals).
    session_totals: dict[str, RunnerResult] = {}
    try:
        for round_no in range(1, rounds + 1):
            if budget > 0:
                reset_calls(workdir)
            resume_id = session_id if resume else None
            fallback = None
            if resume and round_no > 1 and resume_id is None:
                fallback = (
                    "the previous round could not resume its session; this round started a "
                    "fresh session"
                    if lost_session
                    else "no earlier round reported a session id; this round started a fresh "
                    "session"
                )
            if resume_id is not None:
                prompt = build_resume_prompt(
                    task,
                    round_no,
                    rounds,
                    baseline=baseline,
                    best=best,
                    best_round=best_round,
                    unseen=records[session_round - 1 :],
                    budget=budget,
                )
            else:
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
            runner = make_runner(round_dir)
            result = await invoke(
                runner,
                task.model_copy(update={"prompt": prompt}),
                config.model_copy(
                    update={
                        "improve_round": round_no,
                        "resume_session_id": resume_id,
                        "persist_session": resume,
                    }
                ),
            )
            if result.provider_session_id:
                reported = result
                if resume_id == result.provider_session_id and runner.resume_totals_cumulative:
                    result = per_round_totals(result, previous=session_totals.get(resume_id))
                session_totals[result.provider_session_id] = reported
            results.append(result)
            if result.provider_session_id:
                session_id, session_round = result.provider_session_id, round_no
                lost_session = False
            elif resume_id is not None:
                # A resume that reports no session id failed. Resuming the same id again would
                # fail the same way in every later round, so the next round starts afresh.
                session_id, lost_session = None, True
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
                        note=redact("; ".join(n for n in (fallback, result.error) if n)) or None,
                    )
                )
                break
            evaluation = await _evaluate(task, ctx, sandbox, verifier)
            value = evaluation.value if evaluation.gate_passed else None
            accepted = value is not None and is_better(value, best, objective.direction)
            reverted = False
            try:
                if accepted:
                    best, best_round, current = value, round_no, value
                    best_commit = await asyncio.to_thread(
                        snapshot, workdir, ctx.base_commit, f"harnesslab improve round {round_no}"
                    )
                    await asyncio.to_thread(anchor, workdir, anchor_ref, best_commit)
                else:
                    current = value  # until a revert succeeds, the worktree holds this state
                    if spec.keep_best:
                        await asyncio.to_thread(restore, workdir, best_commit)
                        current, reverted = best, True
            except Exception as exc:
                action = "checkpoint" if accepted else "revert"
                stopped = (
                    f"round {round_no}: could not {action} the worktree "
                    f"({type(exc).__name__}: {exc})"
                )
            notes = [fallback, result.error, evaluation.detail, stopped]
            record = RoundRecord(
                round=round_no,
                status=result.status.value,
                value=evaluation.value,
                gate_passed=evaluation.gate_passed,
                best=best,
                accepted=accepted,
                reverted=reverted,
                evaluator_calls=calls,
                note=redact("; ".join(n for n in notes if n)) or None,
            )
            records.append(record)
            emitter.emit(
                EventKind.SYSTEM, name="improve_round", payload=record.model_dump(mode="json")
            )
            if stopped:
                break
    finally:
        drop_anchor(workdir, anchor_ref)

    final = current
    outcome = ImproveResult(
        direction=objective.direction,
        unit=objective.unit,
        target=objective.target,
        keep_best=spec.keep_best,
        session=session,
        rounds_planned=rounds,
        evaluator_budget=budget,
        baseline=baseline,
        best=best,
        best_round=best_round,
        final=final,
        ratio=improvement_ratio(baseline, final, objective.direction),
        score=improvement_score(baseline, final, objective.direction),
        progress=progress(baseline, final, objective.target),
        improved=final is not None
        and is_better(final, baseline, objective.direction, spec.min_improvement),
        evaluator_calls=total_calls,
        rounds=records,
    )
    (artifacts_dir / "improve.json").write_text(outcome.model_dump_json(indent=2), encoding="utf-8")
    merged = merge_runner_results(results, redact)
    if stopped:
        # Harness Lab's own step failed: an infrastructure failure, with nothing thrown away.
        if merged.status == RunStatus.COMPLETED:
            merged.status = RunStatus.CRASHED
        message = f"improvement protocol stopped in {stopped}"
        merged.error = redact("; ".join(m for m in (merged.error, message) if m))
    return merged, outcome

"""Experiment orchestration.

For every (task x variant x repetition) cell the service:

1. inserts a ``running`` run row (so the dashboard can show progress),
2. prepares an isolated worktree via the sandbox,
3. runs the task's setup commands (which must leave the worktree clean),
4. executes the harness runner with the worktree as its cwd,
5. captures ``git status`` / ``git diff --stat`` / ``git diff`` as artifacts,
6. runs the independent verifier in the same worktree,
7. computes metrics, persists events, verdict and artifacts,
8. removes the worktree unless ``keep_worktrees`` is set.

Runs execute concurrently up to ``parallelism``; a failing cell never aborts
the experiment.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

import harnesslab
from harnesslab.config import Settings
from harnesslab.core.events import PARSER_VERSION, EventEmitter, EventKind
from harnesslab.core.ids import hash_value
from harnesslab.core.metrics import compute_metrics
from harnesslab.core.models import (
    METRICS_VERSION,
    DiffSummary,
    EnvironmentSpec,
    ExperimentSpec,
    Outcome,
    RunMetrics,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    SuiteSpec,
    TaskSpec,
    VariantSpec,
    VerifierResult,
)
from harnesslab.core.pricing import PricingTable
from harnesslab.execution.git import git_version, head_commit
from harnesslab.execution.sandbox import ExecutionSandbox, LocalWorktreeSandbox, SandboxContext
from harnesslab.runners.base import HarnessRunner, create_runner
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository
from harnesslab.trace.normalize import close_orphaned_calls
from harnesslab.trace.redaction import Redactor, default_redactor
from harnesslab.verification.command import CommandVerifier

AGENT_TIMEOUT_GRACE_SECONDS = 60


class SetupError(RuntimeError):
    pass


class RunProgress(BaseModel):
    run_id: str
    task_key: str
    variant_key: str
    repetition: int
    phase: str  # started | finished
    status: str | None = None
    outcome: str | None = None
    verified_score: float | None = None
    wall_time_seconds: float | None = None
    error: str | None = None


ProgressCallback = Callable[[RunProgress], None]


class RunOutcome(BaseModel):
    run_id: str
    task_key: str
    variant_key: str
    repetition: int
    status: RunStatus
    outcome: Outcome
    metrics: RunMetrics
    error: str | None = None
    worktree_path: str | None = None


class ExperimentOutcome(BaseModel):
    experiment_id: str
    name: str
    runs: list[RunOutcome]


class ExperimentService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        pricing: PricingTable | None = None,
        sandbox: ExecutionSandbox | None = None,
        runner_factory: Callable[..., HarnessRunner] = create_runner,
        redactor: Redactor | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.repo = Repository(db, settings.home)
        self.pricing = pricing
        self.sandbox = sandbox or LocalWorktreeSandbox(settings)
        self.runner_factory = runner_factory
        self.redactor = redactor or default_redactor()
        self.verifier = CommandVerifier(redactor=self.redactor)
        self.harnesslab_commit = head_commit(Path(harnesslab.__file__).resolve().parent)
        self.harnesslab_version = harnesslab.__version__

    # ------------------------------------------------------------------
    async def run_experiment(
        self,
        experiment: ExperimentSpec,
        suite: SuiteSpec,
        tasks: list[TaskSpec],
        variants: list[VariantSpec],
        *,
        keep_worktrees: bool = False,
        progress: ProgressCallback | None = None,
    ) -> ExperimentOutcome:
        if not tasks:
            raise ValueError("experiment has no tasks")
        if not variants:
            raise ValueError("experiment has no variants")
        self.settings.ensure_dirs()
        environment = EnvironmentSpec.detect(sandbox=self.sandbox.kind, git_version=git_version())

        exp_id = self.repo.create_experiment(
            experiment,
            suite,
            environment=environment,
            harnesslab_version=self.harnesslab_version,
            harnesslab_commit=self.harnesslab_commit,
        )

        # Snapshot fixture repositories once so every run starts from the same commit.
        task_rows: dict[
            str, tuple[str, str, str]
        ] = {}  # task id -> (row id, base_commit, task_hash)
        for position, task in enumerate(tasks):
            snapshot = (
                await asyncio.to_thread(self.sandbox.snapshot, task)
                if hasattr(self.sandbox, "snapshot")
                else None
            )
            base_commit = snapshot.base_commit if snapshot else "unknown"
            task_hash = hash_value({"spec": task.spec_hash(), "base_commit": base_commit})
            row_id = self.repo.add_task(
                exp_id, task, base_commit=base_commit, task_hash=task_hash, position=position
            )
            task_rows[task.id] = (row_id, base_commit, task_hash)

        variant_rows: dict[str, str] = {}
        for position, variant in enumerate(variants):
            variant_rows[variant.id] = self.repo.add_variant(exp_id, variant, position)

        cells = [
            (task, variant, rep)
            for rep in range(max(1, experiment.repetitions))
            for task in tasks
            for variant in variants
        ]
        semaphore = asyncio.Semaphore(max(1, experiment.parallelism))

        async def guarded(task: TaskSpec, variant: VariantSpec, rep: int) -> RunOutcome:
            async with semaphore:
                return await self.execute_run(
                    exp_id=exp_id,
                    task=task,
                    variant=variant,
                    repetition=rep,
                    task_row_id=task_rows[task.id][0],
                    task_hash=task_rows[task.id][2],
                    variant_row_id=variant_rows[variant.id],
                    environment=environment,
                    keep_worktree=keep_worktrees or experiment.keep_worktrees,
                    progress=progress,
                )

        status = "completed"
        try:
            outcomes = await asyncio.gather(*(guarded(t, v, r) for t, v, r in cells))
        except asyncio.CancelledError:
            status = "interrupted"
            self.repo.mark_stale_runs_interrupted()
            raise
        except Exception:
            status = "failed"
            raise
        finally:
            self.repo.finish_experiment(exp_id, status)
        return ExperimentOutcome(experiment_id=exp_id, name=experiment.name, runs=list(outcomes))

    # ------------------------------------------------------------------
    async def execute_run(
        self,
        *,
        exp_id: str,
        task: TaskSpec,
        variant: VariantSpec,
        repetition: int,
        task_row_id: str,
        task_hash: str,
        variant_row_id: str,
        environment: EnvironmentSpec,
        keep_worktree: bool,
        progress: ProgressCallback | None,
    ) -> RunOutcome:
        config: RunnerConfig = variant.runner_config()
        run_id = self.repo.create_run(
            exp_id=exp_id,
            variant_row_id=variant_row_id,
            task_row_id=task_row_id,
            repetition=repetition,
            config=config,
            environment=environment,
            task_hash=task_hash,
            prompt_hash=task.prompt_hash(),
            base_commit=None,
            harnesslab_commit=self.harnesslab_commit,
            harnesslab_version=self.harnesslab_version,
            parser_version=PARSER_VERSION,
            metrics_version=METRICS_VERSION,
        )
        if progress:
            progress(
                RunProgress(
                    run_id=run_id,
                    task_key=task.id,
                    variant_key=variant.id,
                    repetition=repetition,
                    phase="started",
                )
            )

        artifacts_dir = self.settings.artifacts_dir / exp_id / run_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        emitter = EventEmitter(
            run_id, redactor=self.redactor, sink=lambda evs: self.repo.add_events(run_id, evs)
        )

        t0 = time.monotonic()
        runner_result = RunnerResult(status=RunStatus.CRASHED)
        ctx: SandboxContext | None = None
        changes: DiffSummary | None = None
        verifier_result: VerifierResult | None = None
        error: str | None = None
        agent_seconds: float | None = None
        verifier_seconds: float | None = None
        interrupted = False
        wall_seconds: float | None = None

        try:
            try:
                runner = self.runner_factory(config.runner, artifacts_dir=artifacts_dir)
            except KeyError as exc:
                runner = None
                runner_result = RunnerResult(status=RunStatus.UNAVAILABLE, error=str(exc))
                emitter.emit(
                    EventKind.ERROR, name="runner_unavailable", payload={"message": str(exc)}
                )

            if runner is not None:
                availability = await runner.check_availability(config)
                if not availability.available:
                    msg = f"runner {config.runner!r} unavailable: {availability.detail}"
                    runner_result = RunnerResult(status=RunStatus.UNAVAILABLE, error=msg)
                    emitter.emit(
                        EventKind.ERROR, name="runner_unavailable", payload={"message": msg}
                    )
                    runner = None

            if runner is not None:
                # 1. isolated worktree + setup
                ctx = await self.sandbox.prepare(task, experiment_id=exp_id, run_id=run_id)
                self.repo.update_run(
                    run_id, base_commit=ctx.base_commit, worktree_path=str(ctx.workdir)
                )
                await self._run_setup(task, ctx, emitter)
                (artifacts_dir / "prompt.txt").write_text(task.prompt, encoding="utf-8")
                self.repo.add_artifact(run_id, "prompt", artifacts_dir / "prompt.txt")
                emitter.emit(
                    EventKind.RUN_STARTED,
                    payload={
                        "task": task.id,
                        "variant": variant.id,
                        "runner": config.runner,
                        "model": config.model,
                        "repetition": repetition,
                        "base_commit": ctx.base_commit,
                        "worktree": str(ctx.workdir),
                    },
                )

                # 2. the agent
                agent_t0 = time.monotonic()
                try:
                    runner_result = await asyncio.wait_for(
                        runner.run(task, ctx.workdir, config, emitter),
                        timeout=task.limits.agent_timeout_seconds + AGENT_TIMEOUT_GRACE_SECONDS,
                    )
                except TimeoutError:
                    msg = (
                        f"agent exceeded {task.limits.agent_timeout_seconds}s limit and was stopped"
                    )
                    runner_result = RunnerResult(status=RunStatus.TIMEOUT, error=msg)
                    emitter.emit(EventKind.ERROR, name="agent_timeout", payload={"message": msg})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # adapter bug or harness crash: keep the experiment going
                    msg = f"{type(exc).__name__}: {exc}"
                    runner_result = RunnerResult(status=RunStatus.CRASHED, error=msg)
                    emitter.emit(
                        EventKind.ERROR,
                        name="runner_crashed",
                        payload={"message": msg, "traceback": traceback.format_exc()[-4000:]},
                    )
                agent_seconds = time.monotonic() - agent_t0
                close_orphaned_calls(emitter)

                # 3. capture what the agent changed
                changes = await self.sandbox.capture_changes(ctx)
                self._write_change_artifacts(run_id, artifacts_dir, changes)
                self._flag_suite_access(task, emitter, runner_result)

                # 4. independent verification
                verifier_t0 = time.monotonic()
                verifier_result = await self.verifier.verify(task, self.sandbox, ctx, changes)
                verifier_seconds = time.monotonic() - verifier_t0
                self._write_verifier_artifacts(run_id, artifacts_dir, verifier_result)
                emitter.emit(
                    EventKind.SYSTEM,
                    name="verification",
                    payload={
                        "outcome": verifier_result.outcome.value,
                        "exit_code": verifier_result.exit_code,
                        "score": verifier_result.verified_score,
                        "timed_out": verifier_result.timed_out,
                        "protected_violations": verifier_result.protected_violations,
                    },
                )
        except asyncio.CancelledError:
            interrupted = True
            error = "interrupted"
            raise
        except SetupError as exc:
            error = str(exc)
            runner_result = RunnerResult(status=RunStatus.CRASHED, error=error)
            emitter.emit(EventKind.ERROR, name="setup_failed", payload={"message": error})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if runner_result.status in (RunStatus.RUNNING, RunStatus.PENDING, RunStatus.COMPLETED):
                runner_result.status = RunStatus.CRASHED
            runner_result.error = runner_result.error or error
            emitter.emit(
                EventKind.ERROR,
                name="pipeline_error",
                payload={"message": error, "traceback": traceback.format_exc()[-4000:]},
            )
        finally:
            wall_seconds = time.monotonic() - t0
            status = RunStatus.INTERRUPTED if interrupted else runner_result.status
            if status in (RunStatus.PENDING, RunStatus.RUNNING):
                status = RunStatus.COMPLETED
            outcome = verifier_result.outcome if verifier_result else Outcome.NOT_VERIFIED
            error = error or runner_result.error
            emitter.emit(
                EventKind.RUN_FINISHED,
                duration_ms=int(wall_seconds * 1000),
                payload={
                    "status": status.value,
                    "outcome": outcome.value,
                    "error": error,
                    "exit_code": runner_result.exit_code,
                },
            )
            emitter.flush()
            metrics = compute_metrics(
                emitter.events,
                runner_result,
                verifier_result,
                changes,
                wall_time_seconds=round(wall_seconds, 3),
                agent_wall_time_seconds=round(agent_seconds, 3)
                if agent_seconds is not None
                else None,
                verifier_wall_time_seconds=round(verifier_seconds, 3)
                if verifier_seconds is not None
                else None,
                pricing=self.pricing,
            )
            if verifier_result is not None:
                self.repo.save_verifier_result(run_id, verifier_result)
            self.repo.finalize_run(
                run_id,
                status=status,
                outcome=outcome.value,
                runner_result=runner_result,
                metrics=metrics,
                worktree_path=str(ctx.workdir) if ctx else None,
                worktree_kept=bool(ctx and keep_worktree),
                error_message=error,
                events_count=len(emitter.events),
            )
            if ctx is not None:
                try:
                    await asyncio.shield(self.sandbox.cleanup(ctx, keep=keep_worktree))
                except Exception:
                    pass
            if progress:
                progress(
                    RunProgress(
                        run_id=run_id,
                        task_key=task.id,
                        variant_key=variant.id,
                        repetition=repetition,
                        phase="finished",
                        status=status.value,
                        outcome=outcome.value,
                        verified_score=metrics.verified_score,
                        wall_time_seconds=metrics.wall_time_seconds,
                        error=error,
                    )
                )
        return RunOutcome(
            run_id=run_id,
            task_key=task.id,
            variant_key=variant.id,
            repetition=repetition,
            status=status,
            outcome=outcome,
            metrics=metrics,
            error=error,
            worktree_path=str(ctx.workdir) if (ctx and keep_worktree) else None,
        )

    # ------------------------------------------------------------------
    async def _run_setup(self, task: TaskSpec, ctx: SandboxContext, emitter: EventEmitter) -> None:
        if not task.setup.commands:
            return
        for command in task.setup.commands:
            proc = await self.sandbox.run_command(
                ctx, command, timeout=task.setup.timeout_seconds, include_auth=False
            )
            emitter.emit(
                EventKind.SYSTEM,
                name="setup_command",
                duration_ms=proc.duration_ms,
                payload={
                    "command": command,
                    "exit_code": proc.exit_code,
                    "timed_out": proc.timed_out,
                    "stderr_tail": proc.stderr_tail[-2000:],
                },
            )
            if not proc.ok:
                raise SetupError(
                    f"setup command failed ({command!r}): exit={proc.exit_code} timed_out={proc.timed_out} {proc.error or ''}"
                )
        changes = await self.sandbox.capture_changes(ctx)
        if changes.files_changed:
            paths = ", ".join(f.path for f in changes.files[:10])
            raise SetupError(
                "setup commands left the worktree dirty (add generated files to .gitignore): "
                + paths
            )

    def _write_change_artifacts(
        self, run_id: str, artifacts_dir: Path, changes: DiffSummary
    ) -> None:
        files = {
            "agent.diff": (
                "agent_diff",
                "text/x-diff",
                self.redactor.redact_text(changes.diff_text),
            ),
            "git_status.txt": ("git_status", "text/plain", changes.status_text),
            "diff_stat.txt": ("diff_stat", "text/plain", changes.stat_text),
        }
        for filename, (kind, media, content) in files.items():
            path = artifacts_dir / filename
            path.write_text(content, encoding="utf-8")
            self.repo.add_artifact(run_id, kind, path, media)

    def _write_verifier_artifacts(
        self, run_id: str, artifacts_dir: Path, result: VerifierResult
    ) -> None:
        for filename, kind, content in (
            ("verifier_stdout.txt", "verifier_stdout", result.stdout),
            ("verifier_stderr.txt", "verifier_stderr", result.stderr),
        ):
            path = artifacts_dir / filename
            path.write_text(content, encoding="utf-8")
            self.repo.add_artifact(run_id, kind, path, "text/plain")

    @staticmethod
    def _flag_suite_access(
        task: TaskSpec, emitter: EventEmitter, runner_result: RunnerResult
    ) -> None:
        """Flag runs whose shell commands mention the suite directory (possible hidden-test peeking)."""
        needle = str(task.base_dir)
        hits = 0
        for event in emitter.events:
            if event.kind == EventKind.COMMAND_STARTED and needle in str(
                event.payload.get("command", "")
            ):
                hits += 1
        if hits:
            runner_result.metadata["possible_suite_access"] = hits

"""Execution sandboxes.

A sandbox owns *where* a run executes.  The MVP ships
:class:`LocalWorktreeSandbox` (an isolated git worktree on the local machine;
agents still execute code with the user's privileges).  Stronger isolation
(containers, VMs) implements the same interface later - see
:class:`DockerSandbox`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from harnesslab.config import Settings
from harnesslab.core.models import DiffSummary, TaskSpec
from harnesslab.execution.fixture import RepoSnapshot, snapshot_repository
from harnesslab.execution.process import LineCallback, ProcessResult, build_child_env, run_process, shell_argv
from harnesslab.execution.worktree import WorktreeManager, capture_changes


@dataclass
class SandboxContext:
    """Handle to a prepared, isolated working copy for one run."""

    run_id: str
    experiment_id: str
    workdir: Path
    repo_dir: Path
    base_commit: str
    snapshot: RepoSnapshot
    kind: str = "local-worktree"
    extra: dict[str, object] = field(default_factory=dict)


class ExecutionSandbox(ABC):
    kind: str = "abstract"

    @abstractmethod
    async def prepare(self, task: TaskSpec, *, experiment_id: str, run_id: str) -> SandboxContext: ...

    @abstractmethod
    async def run_command(
        self,
        ctx: SandboxContext,
        command: str,
        *,
        timeout: float,
        include_auth: bool = False,
        env_overrides: Mapping[str, str] | None = None,
        env_passthrough: list[str] | None = None,
        on_stdout_line: LineCallback | None = None,
        stderr_path: Path | None = None,
    ) -> ProcessResult: ...

    @abstractmethod
    async def capture_changes(self, ctx: SandboxContext) -> DiffSummary: ...

    @abstractmethod
    async def cleanup(self, ctx: SandboxContext, *, keep: bool = False) -> None: ...


class LocalWorktreeSandbox(ExecutionSandbox):
    kind = "local-worktree"

    def __init__(self, settings: Settings, worktrees: WorktreeManager | None = None) -> None:
        self.settings = settings
        self.worktrees = worktrees or WorktreeManager()

    def snapshot(self, task: TaskSpec) -> RepoSnapshot:
        return snapshot_repository(task.repo_path, task.repo.base_ref, self.settings)

    async def prepare(self, task: TaskSpec, *, experiment_id: str, run_id: str) -> SandboxContext:
        import asyncio

        snapshot = await asyncio.to_thread(self.snapshot, task)
        workdir = self.settings.worktrees_dir / experiment_id / run_id
        await self.worktrees.create(snapshot.repo_dir, snapshot.base_commit, workdir)
        return SandboxContext(
            run_id=run_id,
            experiment_id=experiment_id,
            workdir=workdir,
            repo_dir=snapshot.repo_dir,
            base_commit=snapshot.base_commit,
            snapshot=snapshot,
            kind=self.kind,
        )

    async def run_command(
        self,
        ctx: SandboxContext,
        command: str,
        *,
        timeout: float,
        include_auth: bool = False,
        env_overrides: Mapping[str, str] | None = None,
        env_passthrough: list[str] | None = None,
        on_stdout_line: LineCallback | None = None,
        stderr_path: Path | None = None,
    ) -> ProcessResult:
        env = build_child_env(
            include_auth=include_auth,
            passthrough=env_passthrough or (),
            overrides=env_overrides,
        )
        return await run_process(
            shell_argv(command),
            cwd=ctx.workdir,
            env=env,
            timeout=timeout,
            on_stdout_line=on_stdout_line,
            stderr_path=stderr_path,
        )

    async def capture_changes(self, ctx: SandboxContext) -> DiffSummary:
        return await capture_changes(ctx.workdir, ctx.base_commit)

    async def cleanup(self, ctx: SandboxContext, *, keep: bool = False) -> None:
        if keep:
            return
        await self.worktrees.remove(ctx.repo_dir, ctx.workdir)
        # Remove the (now empty) experiment directory when nothing is left.
        parent = ctx.workdir.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


class DockerSandbox(ExecutionSandbox):
    """Placeholder for container isolation (not implemented in the MVP).

    The interface is the contract: prepare a working copy inside a container,
    execute commands in it, capture changes, tear it down.  Runners will need
    a command-executor abstraction to launch harness CLIs *inside* the
    container; that is tracked in the roadmap.
    """

    kind = "docker"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "DockerSandbox is not implemented yet; use LocalWorktreeSandbox. "
            "See README 'Roadmap' for container isolation."
        )

    async def prepare(self, task: TaskSpec, *, experiment_id: str, run_id: str) -> SandboxContext:  # pragma: no cover
        raise NotImplementedError

    async def run_command(self, ctx: SandboxContext, command: str, **kwargs: object) -> ProcessResult:  # type: ignore[override]  # pragma: no cover
        raise NotImplementedError

    async def capture_changes(self, ctx: SandboxContext) -> DiffSummary:  # pragma: no cover
        raise NotImplementedError

    async def cleanup(self, ctx: SandboxContext, *, keep: bool = False) -> None:  # pragma: no cover
        raise NotImplementedError

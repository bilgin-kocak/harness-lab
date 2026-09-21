"""Verifier interface.

The verifier - never the agent - decides whether a run succeeded.  It runs
*after* the agent process has exited, inside the same isolated worktree, with
an environment that carries no provider credentials.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harnesslab.core.models import DiffSummary, TaskSpec, VerifierResult
from harnesslab.execution.sandbox import ExecutionSandbox, SandboxContext


class Verifier(ABC):
    @abstractmethod
    async def verify(
        self,
        task: TaskSpec,
        sandbox: ExecutionSandbox,
        ctx: SandboxContext,
        changes: DiffSummary,
    ) -> VerifierResult: ...

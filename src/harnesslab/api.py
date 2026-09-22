"""Public Python API for extending Harness Lab.

Write a harness adapter by subclassing :class:`HarnessRunner`, decorate it with
:func:`register_runner`, and make sure the module is imported (list it under
``plugins:`` in a suite/experiment/sweep YAML, pass ``--plugin my_module`` on the
command line, or expose it through the ``harnesslab.runners`` entry-point group
of your package)::

    from pathlib import Path
    from harnesslab.api import (EventKind, EventEmitter, HarnessRunner, RunnerConfig,
                                RunnerResult, RunStatus, TaskSpec, UsageTotals, register_runner)

    @register_runner
    class MyRunner(HarnessRunner):
        name = "my-harness"

        async def run(self, task: TaskSpec, worktree: Path, config: RunnerConfig, emit: EventEmitter) -> RunnerResult:
            emit.emit(EventKind.ASSISTANT_MESSAGE, payload={"text": "hello"})
            ...  # launch your agent with cwd=worktree, translate its output into events
            return RunnerResult(status=RunStatus.COMPLETED, exit_code=0, usage=UsageTotals(input_tokens=10))
"""

from harnesslab.core.events import Event, EventEmitter, EventKind
from harnesslab.core.models import (
    Availability,
    RunnerConfig,
    RunnerResult,
    RunStatus,
    TaskSpec,
    UsageTotals,
    VariantSpec,
)
from harnesslab.execution.process import build_child_env, run_process, shell_argv
from harnesslab.runners.base import HarnessRunner, available_runners, register_runner

__all__ = [
    "Availability",
    "Event",
    "EventEmitter",
    "EventKind",
    "HarnessRunner",
    "RunStatus",
    "RunnerConfig",
    "RunnerResult",
    "TaskSpec",
    "UsageTotals",
    "VariantSpec",
    "available_runners",
    "build_child_env",
    "register_runner",
    "run_process",
    "shell_argv",
]

"""Harness runner interface and registry.

A runner knows how to launch one agent harness against one task inside a
prepared worktree and how to translate the harness's native output into
normalized events.  Runners never touch the database or the UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from harnesslab.core.events import EventEmitter
from harnesslab.core.models import Availability, RunnerConfig, RunnerResult, TaskSpec


class HarnessRunner(ABC):
    """Base class for harness adapters."""

    name: str = "abstract"
    description: str = ""

    def __init__(self, *, artifacts_dir: Path | None = None) -> None:
        # Directory where the runner may write large auxiliary files (raw
        # sanitized streams, stderr logs).  Provided by the experiment service.
        self.artifacts_dir = artifacts_dir

    @abstractmethod
    async def run(
        self,
        task: TaskSpec,
        worktree: Path,
        config: RunnerConfig,
        emit: EventEmitter,
    ) -> RunnerResult:
        """Execute the harness with ``worktree`` as its working directory."""

    async def check_availability(self, config: RunnerConfig | None = None) -> Availability:
        """Report whether the harness can run on this machine (used by ``doctor``)."""
        return Availability(runner=self.name, available=True, detail="built-in")


RunnerFactory = Callable[..., HarnessRunner]

_REGISTRY: dict[str, type[HarnessRunner]] = {}


def register_runner(cls: type[HarnessRunner]) -> type[HarnessRunner]:
    _REGISTRY[cls.name] = cls
    return cls


def get_runner_class(name: str) -> type[HarnessRunner]:
    _ensure_builtin_runners()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown runner {name!r}; known runners: {known}") from exc


def create_runner(name: str, **kwargs: object) -> HarnessRunner:
    return get_runner_class(name)(**kwargs)


def available_runners() -> list[str]:
    _ensure_builtin_runners()
    return sorted(_REGISTRY)


def _ensure_builtin_runners() -> None:
    # Import side effects register the built-in adapters.
    from harnesslab.runners import claude, codex, fake, generic  # noqa: F401

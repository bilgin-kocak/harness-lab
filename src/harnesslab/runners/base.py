"""Harness runner interface and registry.

A runner knows how to launch one agent harness against one task inside a
prepared worktree and how to translate the harness's native output into
normalized events.  Runners never touch the database or the UI.
"""

from __future__ import annotations

import importlib
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from importlib import metadata
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


ENTRY_POINT_GROUP = "harnesslab.runners"
_ENTRY_POINTS_LOADED = False


class PluginError(RuntimeError):
    pass


def load_plugins(modules: Iterable[str]) -> list[str]:
    """Import plugin modules so their ``@register_runner`` decorators run."""
    loaded: list[str] = []
    for name in modules:
        name = name.strip()
        if not name:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # ImportError or an error inside the plugin
            raise PluginError(
                f"could not import plugin module {name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        loaded.append(name)
    return loaded


def _load_entry_point_runners() -> None:
    """Load third-party adapters advertised via the ``harnesslab.runners`` entry-point group."""
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - defensive: broken metadata must not break the CLI
        return
    for ep in entry_points:
        try:
            obj = ep.load()
        except Exception as exc:
            warnings.warn(
                f"harnesslab plugin {ep.name!r} ({ep.value}) failed to load: {exc}", stacklevel=2
            )
            continue
        if isinstance(obj, type) and issubclass(obj, HarnessRunner) and obj.name not in _REGISTRY:
            register_runner(obj)


def _ensure_builtin_runners() -> None:
    # Import side effects register the built-in adapters.
    from harnesslab.runners import claude, codex, fake, generic  # noqa: F401

    _load_entry_point_runners()

"""Optimizer interface and registry.

An optimizer proposes a new harness bundle from the current one and a window of failures.
It never runs anything and never touches the database; the grow service validates, lints,
evaluates and accepts or rejects what it proposes.  Register implementations with
:func:`register_optimizer`, list them under ``plugins:`` in a grow spec, or expose them
through the ``harnesslab.optimizers`` entry-point group.
"""

from __future__ import annotations

import importlib
import warnings
from abc import ABC, abstractmethod
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.core.models import UsageTotals
from harnesslab.runners.base import PluginError

ENTRY_POINT_GROUP = "harnesslab.optimizers"


class OptimizerError(RuntimeError):
    pass


class FailureMetrics(BaseModel):
    llm_calls: int | None = None
    total_tokens: int | None = None
    wall_time_seconds: float | None = None
    reported_cost_usd: float | None = None


class FailureCase(BaseModel):
    """Everything the optimizer may see about one failed task (hidden tests scrubbed)."""

    task_id: str
    task_name: str
    prompt: str
    attempts: int = 0
    run_id: str
    status: str
    outcome: str
    verified_score: float | None = None
    final_message: str = ""
    trace_digest: list[str] = Field(default_factory=list)
    diff: str = ""
    verifier_stdout: str = ""
    verifier_stderr: str = ""
    metrics: FailureMetrics = Field(default_factory=FailureMetrics)


class EditConstraintsSpec(BaseModel):
    allowed_paths: list[str]
    max_files: int
    max_file_bytes: int
    max_bundle_bytes: int


class OptimizerContext(BaseModel):
    session_name: str
    iteration: int
    runner: str
    model: str | None = None
    bundle: dict[str, str]
    constraints: EditConstraintsSpec
    failures: list[FailureCase]
    previous_rejections: list[str] = Field(default_factory=list)
    suite_description: str | None = None


class Proposal(BaseModel):
    """Full content of every file in the candidate bundle (unchanged files included)."""

    files: dict[str, str]
    rationale: str = ""
    usage: UsageTotals = Field(default_factory=UsageTotals)
    cost_usd: float | None = None
    raw: Any = None


class Optimizer(ABC):
    name: str = "abstract"
    description: str = ""

    def __init__(self, options: dict[str, Any], *, artifacts_dir: Path | None = None) -> None:
        self.options = dict(options)
        self.artifacts_dir = artifacts_dir

    @abstractmethod
    async def propose(self, context: OptimizerContext) -> Proposal:
        """Return the candidate bundle's content files."""


_REGISTRY: dict[str, type[Optimizer]] = {}
_ENTRY_POINTS_LOADED = False


def register_optimizer(cls: type[Optimizer]) -> type[Optimizer]:
    _REGISTRY[cls.name] = cls
    return cls


def _load_entry_point_optimizers() -> None:
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
                f"harnesslab optimizer plugin {ep.name!r} ({ep.value}) failed to load: {exc}",
                stacklevel=2,
            )
            continue
        if isinstance(obj, type) and issubclass(obj, Optimizer) and obj.name not in _REGISTRY:
            register_optimizer(obj)


def _ensure_builtin_optimizers() -> None:
    from harnesslab.grow.optimizers import fake  # noqa: F401

    for module in ("harnesslab.grow.optimizers.claude_cli", "harnesslab.grow.optimizers.manual"):
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:  # pragma: no cover - only while modules are absent
            if exc.name != module:
                raise
    _load_entry_point_optimizers()


def get_optimizer_class(name: str) -> type[Optimizer]:
    _ensure_builtin_optimizers()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown optimizer {name!r}; known optimizers: {known}") from exc


def create_optimizer(
    name: str, options: dict[str, Any], *, artifacts_dir: Path | None = None
) -> Optimizer:
    return get_optimizer_class(name)(options, artifacts_dir=artifacts_dir)


def available_optimizers() -> list[str]:
    _ensure_builtin_optimizers()
    return sorted(_REGISTRY)


def load_optimizer_plugins(modules: Iterable[str]) -> list[str]:
    """Import modules so their ``@register_optimizer`` decorators run."""
    loaded: list[str] = []
    for name in modules:
        name = name.strip()
        if not name:
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:
            raise PluginError(
                f"could not import plugin module {name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        loaded.append(name)
    return loaded

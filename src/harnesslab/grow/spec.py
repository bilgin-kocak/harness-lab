"""Grow specifications: the configuration of one Growing Harness session.

A grow spec names a suite, a base variant, an initial harness bundle, how the suite's tasks
are split into train / gate / final sets, the failure-window parameters (K, Q, R_max from the
paper), the optimizer, budgets and what the report minimizes.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from harnesslab.bundled import list_bundled_grows
from harnesslab.core.models import SuiteSpec, TaskSpec
from harnesslab.experiments.spec import (
    SpecError,
    load_suite,
    resolve_suite_reference,
    select_tasks,
)
from harnesslab.harness.bundle import BundleError, HarnessBundle

SPLIT_KEYS = ("train", "gate", "final")


class GrowSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    train: list[str] = Field(default_factory=list)
    gate: list[str] = Field(default_factory=list)
    final: list[str] = Field(default_factory=list)
    fractions: dict[str, float] | None = None
    seed: int = 0


class GrowWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: int = Field(default=4, ge=1)
    min_fixed: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def _q_le_k(self) -> GrowWindow:
        if self.min_fixed > self.size:
            raise ValueError("window.min_fixed must be <= window.size")
        return self


class GrowOptimizerSpec(BaseModel):
    """``kind`` selects the optimizer; every other key is passed to it as an option."""

    model_config = ConfigDict(extra="allow")

    kind: str
    model: str | None = None
    max_files: int = Field(default=6, ge=1)

    @property
    def options(self) -> dict[str, Any]:
        return {"model": self.model, "max_files": self.max_files, **(self.model_extra or {})}


class GrowBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_runs: int | None = None
    max_cost_usd: float | None = None
    max_optimizer_cost_usd: float | None = None


class GrowReportSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimize: Literal["llm_calls", "cost", "tokens", "wall_time"] = "llm_calls"


class GrowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    suite: str
    description: str | None = None
    plugins: list[str] = Field(default_factory=list)
    base_variant: dict[str, Any]
    harness: str | None = None
    split: GrowSplit = Field(default_factory=GrowSplit)
    window: GrowWindow = Field(default_factory=GrowWindow)
    repetitions: int = Field(default=1, ge=1)
    parallelism: int = Field(default=1, ge=1)
    max_iterations: int = Field(default=10, ge=1)
    optimizer: GrowOptimizerSpec
    budget: GrowBudget | None = None
    report: GrowReportSpec = Field(default_factory=GrowReportSpec)
    keep_worktrees: bool = False
    source_path: Path | None = Field(default=None, exclude=True)
    harness_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _needs_runner(self) -> GrowSpec:
        if "runner" not in self.base_variant:
            raise ValueError("base_variant must name a runner")
        return self

    @property
    def base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()


class ResolvedSplit(BaseModel):
    train: list[str]
    gate: list[str]
    final: list[str] = Field(default_factory=list)


def resolve_split(spec: GrowSpec, task_ids: list[str]) -> ResolvedSplit:
    """Turn ``spec.split`` into concrete task-id lists (explicit lists or seeded fractions)."""
    s = spec.split
    if s.fractions:
        unknown = set(s.fractions) - set(SPLIT_KEYS)
        if unknown:
            raise SpecError(f"split.fractions has unknown keys: {sorted(unknown)}")
        if sum(s.fractions.values()) > 1.0 + 1e-9:
            raise SpecError("split.fractions must sum to at most 1")
        ids = list(task_ids)
        random.Random(s.seed).shuffle(ids)
        n = len(ids)
        n_train = max(1, round(s.fractions.get("train", 0.0) * n))
        n_gate = max(1, round(s.fractions.get("gate", 0.0) * n))
        if n_train + n_gate > n:
            raise SpecError(f"split.fractions need at least 2 tasks (have {n})")
        n_final = max(0, min(n - n_train - n_gate, round(s.fractions.get("final", 0.0) * n)))
        return ResolvedSplit(
            train=ids[:n_train],
            gate=ids[n_train : n_train + n_gate],
            final=ids[n_train + n_gate : n_train + n_gate + n_final],
        )
    known = set(task_ids)
    for name, ids in (("train", s.train), ("gate", s.gate), ("final", s.final)):
        missing = [t for t in ids if t not in known]
        if missing:
            raise SpecError(f"split.{name} has unknown task ids: {', '.join(missing)}")
    if not s.train or not s.gate:
        raise SpecError("split.train and split.gate must be non-empty")
    combined = s.train + s.gate + s.final
    if len(set(combined)) != len(combined):
        raise SpecError("split lists overlap or contain duplicates")
    return ResolvedSplit(train=list(s.train), gate=list(s.gate), final=list(s.final))


def resolve_grow_target(ref: str | Path) -> Path:
    """A grow YAML path or the name of a bundled grow template (e.g. ``demo-fake``)."""
    path = Path(ref).expanduser()
    if path.exists():
        return path.resolve()
    bundled = list_bundled_grows()
    if str(ref) in bundled:
        return bundled[str(ref)]
    names = ", ".join(sorted(bundled)) or "none"
    raise SpecError(
        f"grow spec not found: {ref!r} is neither a file nor a bundled grow spec (bundled: {names})"
    )


def load_grow(path: Path) -> GrowSpec:
    path = Path(path).resolve()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpecError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    try:
        spec = GrowSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid grow spec {path}:\n{exc}") from exc
    spec.source_path = path
    if spec.harness:
        hpath = Path(spec.harness).expanduser()
        if not hpath.is_absolute():
            hpath = spec.base_dir / hpath
        if not hpath.is_dir():
            raise SpecError(f"grow spec {spec.name}: harness bundle not found: {hpath}")
        try:
            HarnessBundle.load(hpath)
        except BundleError as exc:
            raise SpecError(f"grow spec {spec.name}: {exc}") from exc
        spec.harness_dir = hpath.resolve()
    return spec


def load_grow_target(
    target: str | Path,
) -> tuple[GrowSpec, SuiteSpec, list[TaskSpec], ResolvedSplit]:
    """Load a grow spec, its suite, the tasks the split uses (in split order) and the split."""
    spec = load_grow(resolve_grow_target(target))
    suite, tasks = load_suite(resolve_suite_reference(spec.suite, spec.base_dir))
    split = resolve_split(spec, [t.id for t in tasks])
    return spec, suite, select_tasks(tasks, split.train + split.gate + split.final), split

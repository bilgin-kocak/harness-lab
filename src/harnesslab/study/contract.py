"""The domain adapter contract: the only interface between the core and a domain.

A domain adapter owns its tasks, the settings that can change, the system under test, the
independent verifier and the domain's metrics. The core owns everything else: splits, search,
budgets, scheduling, the freeze record, the held-out evaluation, statistics and storage. The core
never interprets task data, knob levels, outputs or metric meanings; it only moves them around,
hashes them and compares numbers.

An adapter implements six methods (see :class:`DomainAdapter`):

``load_tasks(ids)``
    Exactly the tasks named, each with a content hash. The core passes only the IDs of the
    current phase: a search never sees held-out IDs.
``search_space()``
    Named knobs with finite JSON levels. Pure.
``baseline_config()``
    The configuration everything is compared against; a point of the search space. Pure.
``run(task, config, ctx)``
    Runs the system under test once and returns an :class:`Attempt`: the candidate output and
    what it cost. It never decides success. It must not spend more than ``ctx.cost_limit_usd``.
``verify(task, attempt, ctx)``
    The independent check, returning a :class:`Verdict`. It never treats the system's own
    claims as evidence. ``unverifiable`` is not a pass.
``metrics(task, attempt, verdict)``
    Domain numbers for one attempt. Pure: the same inputs always give the same numbers.

Everything here is plain data (``pydantic``) or the standard library.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_json(value: Any) -> str:
    """JSON with sorted keys and no whitespace: equal values give equal text."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Task(BaseModel):
    """One task. ``data`` belongs to the adapter; the core only stores and hashes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    content_hash: str  # SHA-256 of the task's source, as recorded in the split file
    data: dict[str, Any] = Field(default_factory=dict)
    tags: tuple[str, ...] = ()


class Knob(BaseModel):
    """A setting that can change, with its finite list of JSON levels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    levels: tuple[Any, ...]
    description: str = ""

    @model_validator(mode="after")
    def _levels(self) -> Knob:
        if not self.levels:
            raise ValueError(f"knob {self.name!r} has no levels")
        keys = [canonical_json(level) for level in self.levels]
        if len(set(keys)) != len(keys):
            raise ValueError(f"knob {self.name!r} has duplicate levels")
        return self

    def has_level(self, value: Any) -> bool:
        key = canonical_json(value)
        return any(canonical_json(level) == key for level in self.levels)


class ConfigError(ValueError):
    """A configuration is not a point of the search space: a knob missing or unknown, or a value
    that is not one of its knob's levels."""


class SearchSpace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    knobs: tuple[Knob, ...]

    @model_validator(mode="after")
    def _unique(self) -> SearchSpace:
        names = [k.name for k in self.knobs]
        if len(set(names)) != len(names):
            raise ValueError("knob names must be unique")
        return self

    @property
    def names(self) -> list[str]:
        return [k.name for k in self.knobs]

    def knob(self, name: str) -> Knob:
        for k in self.knobs:
            if k.name == name:
                return k
        raise KeyError(f"unknown knob {name!r} (knobs: {', '.join(self.names)})")

    @property
    def size(self) -> int:
        return math.prod(len(k.levels) for k in self.knobs)

    def validate_config(self, config: Config, *, off_grid: Sequence[str] = ()) -> None:
        """Every knob set, no unknown knob, every value a level (except knobs in ``off_grid``);
        raises :class:`ConfigError` otherwise."""
        missing = [n for n in self.names if n not in config.values]
        unknown = [n for n in config.values if n not in self.names]
        if missing or unknown:
            raise ConfigError(
                f"config {config.label or config.config_hash[:12]}: missing knobs "
                f"{missing}, unknown knobs {unknown}"
            )
        for name, value in config.values.items():
            if name in off_grid:
                continue
            if not self.knob(name).has_level(value):
                raise ConfigError(
                    f"config {config.label or config.config_hash[:12]}: {value!r} "
                    f"is not a level of knob {name!r}"
                )


class Config(BaseModel):
    """A point of the search space. ``label`` is a human name and is not part of the hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: dict[str, Any]
    label: str | None = None

    @property
    def config_hash(self) -> str:
        return sha256_json(self.values)

    def replace(self, *, label: str | None = None, **changes: Any) -> Config:
        return Config(values={**self.values, **changes}, label=label)


class Usage(BaseModel):
    """What an attempt consumed. Adapters fill what applies; the rest stays zero."""

    model_config = ConfigDict(extra="forbid")

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    llm_calls: int = 0


class AttemptStatus(StrEnum):
    COMPLETED = "completed"  # the system ran to its own end (success or not is the verifier's call)
    CAPPED = "capped"  # stopped by a per-attempt limit (cost, calls or time)
    TIMEOUT = "timeout"
    REFUSED = "refused"  # a model declined; counted, never retried on another model
    ERROR = "error"
    # The system could not start at all: the core records no outcome (a harness error) and runs
    # the cell again later; it is never verified or counted as a failure.
    UNAVAILABLE = "unavailable"


class Attempt(BaseModel):
    """One run of the system under test on one task with one configuration.

    ``output`` is small JSON that identifies what was produced (hashes, counts, relative artifact
    paths) and is stored with the record. ``payload`` is the in-memory artifact handed to
    ``verify`` in the same process; it is never serialized.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    task_id: str
    config_hash: str
    repetition: int
    status: AttemptStatus
    output: dict[str, Any] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    wall_time_s: float | None = None
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    payload: Any = Field(default=None, exclude=True)


class VerdictStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"  # could not be checked (too large, checker timed out): not a pass


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: VerdictStatus
    reason: str = ""
    checker: str  # name and version of the check that decided
    details: dict[str, Any] = Field(default_factory=dict)
    duration_s: float | None = None

    @property
    def passed(self) -> bool:
        return self.status == VerdictStatus.PASS


Metrics = dict[str, float | int | bool | None]


class RunContext(BaseModel):
    """What the core tells an adapter about one cell (task × config × repetition)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    study: str
    phase: str  # "smoke", "dev" or "held_out"
    repetition: int
    attempt_dir: Path  # private to this cell; created by the core
    keep_artifacts: bool  # write outputs worth keeping under attempt_dir
    cost_limit_usd: float | None  # hard per-attempt cap; the core has reserved this much
    seed: int  # deterministic per cell, for adapters that need their own randomness


class VerifyContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    study: str
    phase: str
    attempt_dir: Path
    keep_artifacts: bool


@runtime_checkable
class DomainAdapter(Protocol):
    """The six methods every domain implements (see the module docstring)."""

    name: str
    version: str

    def load_tasks(self, ids: Sequence[str]) -> list[Task]: ...

    def search_space(self) -> SearchSpace: ...

    def baseline_config(self) -> Config: ...

    async def run(self, task: Task, config: Config, ctx: RunContext) -> Attempt: ...

    async def verify(self, task: Task, attempt: Attempt, ctx: VerifyContext) -> Verdict: ...

    def metrics(self, task: Task, attempt: Attempt, verdict: Verdict) -> Metrics: ...


class DomainAdapterBase:
    """Optional base class: defaults for the optional parts of an adapter.

    ``paid``: runs spend money (the CLI asks before starting them). ``provenance()``: pins worth
    recording (dataset commit, toolchain and model versions). ``audit_split()``: domain-level
    leakage checks; returns problems, empty when clean. ``close()``: release resources.
    """

    name: str = "abstract"
    version: str = "0"
    paid: bool = False

    def provenance(self) -> dict[str, Any]:
        return {}

    def audit_split(self, dev: list[Task], held_out_ids: Sequence[str]) -> list[str]:
        return []

    async def close(self) -> None:
        return None

"""Study specifications: what a study runs, in which order, within which budget.

A study spec is YAML next to the study's ``split.json``. It names the adapter and its options, where
the dev and held-out IDs are in the split file, the search stages, the objective, the budget, the
held-out comparators and the statistics settings. Nothing in it is domain knowledge: knob names
and metric names are strings the adapter defines.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Amounts of money and time: never negative, never NaN or infinite.
Amount = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class StudySpecError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SplitSpec(_Strict):
    """Where the task IDs are in the split file.

    ``dev`` and ``held_out`` are dotted paths into the split JSON (``"dev"``,
    ``"assignment.A.held_out"``); each must resolve to a list of IDs or of objects with an ``id``.
    ``hashes`` are paths to lists of objects with ``id`` and ``sha256``: the content hashes the
    tasks must still have when they are loaded.
    """

    file: str = "split.json"
    dev: list[str]
    held_out: list[str]
    hashes: list[str] = Field(default_factory=list)


class ConfigSpec(_Strict):
    """A named configuration: a base configuration with some knobs set."""

    label: str
    base: str = "baseline"
    set: dict[str, Any] = Field(default_factory=dict)


class StageSpec(_Strict):
    """One step of the search, run on dev tasks.

    kinds:
      ``explicit``: the listed ``configs``.
      ``grid``: every combination of the levels of ``knobs``, the other knobs from ``base``.
      ``refine``: the ``top_k`` best configurations so far, each crossed with every combination of
      the levels of ``knobs``.
      For both, ``levels`` may restrict a knob to some of its levels (a subset of the adapter's).
      ``combine``: ``reference`` plus every single-knob addition that beat it by at least
      ``min_gain`` (objective score), when at least ``min_winners`` such additions exist. An
      addition sets one knob the reference leaves at the baseline's level; undoing or changing a
      knob the reference already moved is not one. The combination keeps the reference's settings.
      ``confirm``: the ``top_k`` best configurations so far (and the baseline), with
      ``repetitions`` more repetitions each.
    ``phase: smoke`` records into a separate phase that selection never reads.
    ``if_budget_allows``: run only if the projected cost of the stage fits the budget left for
    search after the held-out reserve.
    """

    name: str
    kind: Literal["explicit", "grid", "refine", "combine", "confirm"]
    repetitions: int = Field(default=1, ge=1)
    configs: list[ConfigSpec] = Field(default_factory=list)
    knobs: list[str] = Field(default_factory=list)
    levels: dict[str, list[Any]] = Field(default_factory=dict)
    base: str = "baseline"
    top_k: int = Field(default=0, ge=0)
    reference: str | None = None
    min_gain: float = 0.0
    min_winners: int = Field(default=2, ge=1)
    include_baseline: bool = True
    if_budget_allows: bool = False
    tasks: list[str] | None = None  # a subset of the dev IDs (for a smoke test)
    # "baseline_verified": only the dev tasks the objective keeps after dropping those where the
    # baseline did not verify (the baseline must already have a record on every dev task).
    tasks_from: Literal["all", "baseline_verified"] = "all"
    phase: Literal["dev", "smoke"] = "dev"

    @model_validator(mode="after")
    def _kind_fields(self) -> StageSpec:
        if self.kind == "explicit" and not self.configs:
            raise ValueError(f"stage {self.name!r}: explicit stages need configs")
        if self.kind in ("grid", "refine") and not self.knobs:
            raise ValueError(f"stage {self.name!r}: {self.kind} stages need knobs")
        if set(self.levels) - set(self.knobs):
            raise ValueError(f"stage {self.name!r}: levels given for knobs the stage does not vary")
        if any(not v for v in self.levels.values()):
            raise ValueError(f"stage {self.name!r}: a knob restricted to no level")
        if self.kind not in ("explicit",) and self.configs:
            raise ValueError(f"stage {self.name!r}: only explicit stages take configs")
        if self.kind != "combine" and self.reference is not None:
            raise ValueError(f"stage {self.name!r}: only combine stages take a reference")
        if self.kind in ("refine", "confirm") and self.top_k < 1:
            raise ValueError(f"stage {self.name!r}: {self.kind} stages need top_k >= 1")
        if self.kind == "combine" and not self.reference:
            raise ValueError(f"stage {self.name!r}: combine stages need a reference label")
        if self.phase == "smoke" and self.kind in ("refine", "combine", "confirm"):
            raise ValueError(f"stage {self.name!r}: a smoke stage must be explicit or grid")
        return self


class SearchSpec(_Strict):
    seed: int = 0  # evaluation order of configurations within a stage
    stages: list[StageSpec]

    @model_validator(mode="after")
    def _unique(self) -> SearchSpec:
        names = [s.name for s in self.stages]
        if len(set(names)) != len(names):
            raise ValueError("stage names must be unique")
        return self


class ObjectiveSpec(_Strict):
    """How dev results rank configurations.

    ``pass_rate``: maximise the mean per-task pass rate (repetitions averaged within a task);
    feasible only if the mean cost per attempt is at most ``cost_cap_multiple`` times the
    baseline's; configurations within ``tie_tolerance`` of the best rate are ordered by cost per
    passed attempt.
    ``baseline_ratio``: feasible only if every considered dev task passed; minimise, in order,
    the geometric mean over tasks of ``(metric + offset) / (baseline metric + offset)`` for each of
    ``metrics`` (equal within ``tie_tolerance`` goes to the next), then the median of each of
    ``then``. Tasks whose baseline did not pass are dropped first when
    ``drop_tasks_where_baseline_unverified`` is set.
    """

    kind: Literal["pass_rate", "baseline_ratio"]
    cost_cap_multiple: Amount | None = None
    tie_tolerance: Amount = 0.0
    metrics: list[str] = Field(default_factory=list)
    offset: Amount = 1.0
    then: list[str] = Field(default_factory=list)
    drop_tasks_where_baseline_unverified: bool = True

    @model_validator(mode="after")
    def _fields(self) -> ObjectiveSpec:
        if self.kind == "baseline_ratio" and not self.metrics:
            raise ValueError("a baseline_ratio objective needs metrics")
        if self.kind == "pass_rate" and (self.metrics or self.then):
            raise ValueError("a pass_rate objective takes no metrics")
        if self.kind == "baseline_ratio" and self.cost_cap_multiple is not None:
            raise ValueError("cost_cap_multiple applies to pass_rate objectives only")
        return self


class BudgetSpec(_Strict):
    """Limits for the whole study (smoke, dev and held-out phases together).

    ``cost_limit_per_attempt_usd`` is reserved before each cell starts and passed to the adapter
    as a hard cap, so cells in flight can never take the study over ``max_cost_usd`` (an adapter
    may state a lower worst case per configuration, which is then reserved instead).
    ``held_out_reserve_usd`` is never spent by the search. ``max_attempts`` and
    ``max_cell_seconds`` bound the search (smoke and dev phases); the held-out phase is bounded by
    money only, since the freeze fixes its size.
    """

    max_cost_usd: Amount | None = None
    max_attempts: int | None = Field(default=None, ge=0)
    max_cell_seconds: Amount | None = None
    cost_limit_per_attempt_usd: Amount | None = None
    held_out_reserve_usd: Amount = 0.0

    @model_validator(mode="after")
    def _cost(self) -> BudgetSpec:
        if self.max_cost_usd is not None and self.cost_limit_per_attempt_usd is None:
            raise ValueError(
                "max_cost_usd needs cost_limit_per_attempt_usd (the per-cell reservation)"
            )
        if self.max_cost_usd is not None and self.held_out_reserve_usd > self.max_cost_usd:
            raise ValueError("held_out_reserve_usd is larger than max_cost_usd")
        return self


class ComparatorSpec(_Strict):
    label: str
    base: Literal["baseline", "frozen"] = "baseline"
    set: dict[str, Any] = Field(default_factory=dict)


class RobustnessSpec(_Strict):
    """Re-run ``configs`` with ``knob`` replaced by each of ``levels`` (levels may be off-grid)."""

    knob: str
    levels: list[Any] = Field(min_length=1)
    configs: list[Literal["baseline", "frozen"]] = Field(
        default_factory=lambda: ["baseline", "frozen"]
    )


class FixedFrozenSpec(_Strict):
    """A frozen configuration fixed before any data: the baseline with ``set``."""

    label: str
    set: dict[str, Any] = Field(default_factory=dict)


class HeldOutSpec(_Strict):
    """The one held-out comparison.

    ``fixed_frozen`` makes the study confirmatory: the freeze takes this configuration instead of
    ranking the dev records, which then only check the setup. Without it, the freeze takes the best
    configuration on dev, as before.
    """

    repetitions: int = Field(default=1, ge=1)
    comparators: list[ComparatorSpec] = Field(default_factory=list)
    robustness: RobustnessSpec | None = None
    order_seed: int = 0
    require_committed_freeze: bool = True
    fixed_frozen: FixedFrozenSpec | None = None


class StatsSpec(_Strict):
    resamples: int = Field(default=10_000, ge=100)
    seed: int = 0
    level: float = Field(default=0.95, gt=0.0, lt=1.0)
    report_metrics: list[str] = Field(
        default_factory=list
    )  # compared in the report, lower is better
    higher_is_better: list[str] = Field(default_factory=list)
    # Held-out comparisons are also reported per task tag listed here (e.g. a pool).
    tag_groups: list[str] = Field(default_factory=list)


class StudySpec(_Strict):
    name: str
    adapter: str
    adapter_options: dict[str, Any] = Field(default_factory=dict)
    split: SplitSpec
    parallelism: int = Field(default=1, ge=1)
    keep_dev_artifacts: bool = False
    search: SearchSpec
    objective: ObjectiveSpec
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    held_out: HeldOutSpec = Field(default_factory=HeldOutSpec)
    stats: StatsSpec = Field(default_factory=StatsSpec)
    source_path: Path | None = Field(default=None, exclude=True)

    @property
    def study_dir(self) -> Path:
        return self.source_path.parent if self.source_path else Path.cwd()

    @model_validator(mode="after")
    def _consistent(self) -> StudySpec:
        for stage in self.search.stages:
            if stage.tasks_from == "baseline_verified" and self.objective.kind != "baseline_ratio":
                raise ValueError(
                    f"stage {stage.name!r}: tasks_from baseline_verified needs a baseline_ratio "
                    "objective"
                )
        return self

    def stage(self, name: str) -> StageSpec:
        for s in self.search.stages:
            if s.name == name:
                return s
        raise StudySpecError(f"no stage named {name!r} in study {self.name!r}")


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that rejects a key repeated in one mapping (YAML would keep the last)."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _unique_mapping,  # type: ignore[arg-type]
)


def load_study_spec(path: Path) -> StudySpec:
    path = Path(path).resolve()
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # noqa: S506
    except FileNotFoundError as exc:
        raise StudySpecError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise StudySpecError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StudySpecError(f"{path}: expected a mapping at the top level")
    try:
        spec = StudySpec(**data)
    except ValidationError as exc:
        raise StudySpecError(f"invalid study spec {path}:\n{exc}") from exc
    spec.source_path = path
    return spec


def spec_sha256(spec: StudySpec) -> str:
    if spec.source_path is None:
        return hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
    return hashlib.sha256(spec.source_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Split files
# ---------------------------------------------------------------------------


def _walk(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise StudySpecError(f"split path {path!r}: no key {part!r}")
        node = node[part]
    return node


def _ids(node: Any, path: str) -> list[str]:
    if not isinstance(node, list):
        raise StudySpecError(f"split path {path!r} is not a list")
    out = []
    for item in node:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            out.append(item["id"])
        else:
            raise StudySpecError(f"split path {path!r}: items must be IDs or objects with an id")
    return out


class Split(BaseModel):
    """The resolved split: fixed dev and held-out IDs and the hashes recorded for them."""

    dev: list[str]
    held_out: list[str]
    recorded_hashes: dict[str, str]
    file_sha256: str

    @property
    def dev_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.dev).encode()).hexdigest()

    @property
    def held_out_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.held_out).encode()).hexdigest()


def load_split(spec: StudySpec) -> Split:
    path = spec.study_dir / spec.split.file
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise StudySpecError(f"split file not found: {path}") from exc
    data = json.loads(raw)
    dev = [i for p in spec.split.dev for i in _ids(_walk(data, p), p)]
    held = [i for p in spec.split.held_out for i in _ids(_walk(data, p), p)]
    hashes: dict[str, str] = {}
    for p in spec.split.hashes:
        node = _walk(data, p)
        if not isinstance(node, list):
            raise StudySpecError(f"split path {p!r} is not a list")
        for item in node:
            if isinstance(item, dict) and "id" in item and "sha256" in item:
                hashes[item["id"]] = item["sha256"]
    for name, ids in (("dev", dev), ("held_out", held)):
        if len(set(ids)) != len(ids):
            raise StudySpecError(f"split {name} list has duplicate IDs")
    return Split(
        dev=dev,
        held_out=held,
        recorded_hashes=hashes,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    )

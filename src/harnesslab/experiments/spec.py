"""Loading task, suite and experiment YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from harnesslab.bundled import list_bundled_suites, list_bundled_sweeps
from harnesslab.core.models import ExperimentSpec, SuiteSpec, SweepSpec, TaskSpec, VariantSpec
from harnesslab.harness.bundle import BundleError, HarnessBundle

# Variants available even when a suite does not define any, so the quickstart
# commands work out of the box.
BUILTIN_VARIANTS: list[VariantSpec] = [
    VariantSpec(
        id="fake-reference",
        runner="fake",
        description="Deterministic fake agent applying the reference solution",
        behavior="solve",
    ),
    VariantSpec(
        id="fake-noop",
        runner="fake",
        description="Fake agent that changes nothing (control)",
        behavior="noop",
    ),
    VariantSpec(
        id="codex-default",
        runner="codex",
        model=None,
        description="OpenAI Codex CLI, default model, workspace-write sandbox",
    ),
    VariantSpec(
        id="claude-default",
        runner="claude",
        model=None,
        description="Claude Code CLI, default model, acceptEdits",
        max_turns=30,
    ),
]


class SpecError(ValueError):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpecError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    return data


def resolve_suite_target(ref: str | Path) -> Path:
    """Accept a path to a suite/experiment YAML or the name of a bundled suite (e.g. ``demo``)."""
    path = Path(ref).expanduser()
    if path.exists():
        return path.resolve()
    bundled = list_bundled_suites()
    if str(ref) in bundled:
        return bundled[str(ref)]
    names = ", ".join(sorted(bundled)) or "none"
    raise SpecError(
        f"suite not found: {ref!r} is neither a file nor a bundled suite (bundled: {names})"
    )


def resolve_variant_harness(variant: VariantSpec, base_dir: Path) -> None:
    """Resolve ``variant.harness`` (relative to ``base_dir``) and record the bundle hash."""
    if not variant.harness:
        return
    path = Path(variant.harness).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_dir():
        raise SpecError(f"variant {variant.id!r}: harness bundle not found: {path}")
    try:
        bundle = HarnessBundle.load(path)
    except BundleError as exc:
        raise SpecError(f"variant {variant.id!r}: {exc}") from exc
    variant.harness_dir = path.resolve()
    variant.harness_hash = bundle.hash


def resolve_variant_harnesses(variants: list[VariantSpec], base_dir: Path) -> None:
    for variant in variants:
        resolve_variant_harness(variant, base_dir)


def load_task(path: Path) -> TaskSpec:
    path = Path(path).resolve()
    data = _read_yaml(path)
    try:
        task = TaskSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid task spec {path}:\n{exc}") from exc
    task.source_path = path
    if not task.repo_path.exists():
        raise SpecError(f"task {task.id}: repository path does not exist: {task.repo_path}")
    return task


def load_suite(path: Path) -> tuple[SuiteSpec, list[TaskSpec]]:
    path = Path(path).resolve()
    data = _read_yaml(path)
    try:
        suite = SuiteSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid suite spec {path}:\n{exc}") from exc
    suite.source_path = path
    tasks: list[TaskSpec] = []
    seen: set[str] = set()
    for entry in suite.tasks:
        task_path = Path(entry)
        if not task_path.is_absolute():
            task_path = suite.base_dir / task_path
        task = load_task(task_path)
        if task.id in seen:
            raise SpecError(f"duplicate task id {task.id!r} in suite {suite.name}")
        seen.add(task.id)
        tasks.append(task)
    resolve_variant_harnesses(suite.variants, suite.base_dir)
    return suite, tasks


def load_experiment(path: Path) -> ExperimentSpec:
    path = Path(path).resolve()
    data = _read_yaml(path)
    try:
        spec = ExperimentSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid experiment spec {path}:\n{exc}") from exc
    spec.source_path = path
    resolve_variant_harnesses(spec.variants, spec.base_dir)
    return spec


def is_experiment_file(path: Path) -> bool:
    data = _read_yaml(Path(path))
    return "suite" in data and "tasks" not in data


def resolve_suite_reference(ref: str, base_dir: Path) -> Path:
    """Resolve a ``suite:`` reference from an experiment/sweep file (relative path or bundled name)."""
    candidate = Path(ref).expanduser()
    if not candidate.is_absolute():
        relative = base_dir / candidate
        if relative.exists():
            return relative.resolve()
    return resolve_suite_target(ref)


def load_run_target(target: str | Path) -> tuple[ExperimentSpec, SuiteSpec, list[TaskSpec]]:
    """Accept a suite YAML, an experiment YAML, or a bundled suite name."""
    path = resolve_suite_target(target)
    if is_experiment_file(path):
        exp = load_experiment(path)
        suite, tasks = load_suite(resolve_suite_reference(exp.suite, exp.base_dir))
    else:
        suite, tasks = load_suite(path)
        exp = ExperimentSpec(name=suite.name, suite=str(path), variants=[], source_path=path)
    return exp, suite, tasks


def resolve_variants(
    requested: list[str] | None,
    experiment: ExperimentSpec,
    suite: SuiteSpec,
) -> list[VariantSpec]:
    """Pick variants by id from experiment spec > suite spec > built-ins."""
    catalogue: dict[str, VariantSpec] = {}
    for v in BUILTIN_VARIANTS:
        catalogue[v.id] = v
    for v in suite.variants:
        catalogue[v.id] = v
    for v in experiment.variants:
        catalogue[v.id] = v
    if requested:
        chosen: list[VariantSpec] = []
        for vid in requested:
            if vid not in catalogue:
                raise SpecError(
                    f"unknown variant {vid!r}; available: {', '.join(sorted(catalogue))}"
                )
            chosen.append(catalogue[vid])
        return chosen
    if experiment.variants:
        return list(experiment.variants)
    if suite.variants:
        return list(suite.variants)
    return [catalogue["fake-reference"]]


def select_tasks(tasks: list[TaskSpec], requested: list[str] | None) -> list[TaskSpec]:
    if not requested:
        return tasks
    by_id = {t.id: t for t in tasks}
    missing = [t for t in requested if t not in by_id]
    if missing:
        raise SpecError(f"unknown task id(s): {', '.join(missing)}; available: {', '.join(by_id)}")
    return [by_id[t] for t in requested]


def resolve_sweep_target(ref: str | Path) -> Path:
    """A sweep YAML path or the name of a bundled sweep template (e.g. ``demo-fake``)."""
    path = Path(ref).expanduser()
    if path.exists():
        return path.resolve()
    bundled = list_bundled_sweeps()
    if str(ref) in bundled:
        return bundled[str(ref)]
    names = ", ".join(sorted(bundled)) or "none"
    raise SpecError(
        f"sweep not found: {ref!r} is neither a file nor a bundled sweep (bundled: {names})"
    )


def load_sweep(path: Path) -> SweepSpec:
    path = Path(path).resolve()
    data = _read_yaml(path)
    try:
        spec = SweepSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid sweep spec {path}:\n{exc}") from exc
    spec.source_path = path
    return spec


def load_sweep_target(target: str | Path) -> tuple[SweepSpec, SuiteSpec, list[TaskSpec]]:
    """Load a sweep and the suite it refers to (tasks filtered by ``tasks:`` if given)."""
    spec = load_sweep(resolve_sweep_target(target))
    suite, tasks = load_suite(resolve_suite_reference(spec.suite, spec.base_dir))
    tasks = select_tasks(tasks, spec.tasks)
    missing = [t for t in spec.holdout_tasks if t not in {task.id for task in tasks}]
    if missing:
        raise SpecError(f"holdout_tasks not in suite: {', '.join(missing)}")
    return spec, suite, tasks

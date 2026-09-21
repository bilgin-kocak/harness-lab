"""Loading task, suite and experiment YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from harnesslab.core.models import ExperimentSpec, SuiteSpec, TaskSpec, VariantSpec

# Variants available even when a suite does not define any, so the quickstart
# commands work out of the box.
BUILTIN_VARIANTS: list[VariantSpec] = [
    VariantSpec(id="fake-reference", runner="fake", description="Deterministic fake agent applying the reference solution", behavior="solve"),
    VariantSpec(id="fake-noop", runner="fake", description="Fake agent that changes nothing (control)", behavior="noop"),
    VariantSpec(id="codex-default", runner="codex", model=None, description="OpenAI Codex CLI, default model, workspace-write sandbox"),
    VariantSpec(id="claude-default", runner="claude", model=None, description="Claude Code CLI, default model, acceptEdits", max_turns=30),
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
    return suite, tasks


def load_experiment(path: Path) -> ExperimentSpec:
    path = Path(path).resolve()
    data = _read_yaml(path)
    try:
        spec = ExperimentSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"invalid experiment spec {path}:\n{exc}") from exc
    spec.source_path = path
    return spec


def is_experiment_file(path: Path) -> bool:
    data = _read_yaml(Path(path))
    return "suite" in data and "tasks" not in data


def load_run_target(path: Path) -> tuple[ExperimentSpec, SuiteSpec, list[TaskSpec]]:
    """Accept either a suite YAML or an experiment YAML."""
    path = Path(path).resolve()
    if is_experiment_file(path):
        exp = load_experiment(path)
        suite_path = Path(exp.suite)
        if not suite_path.is_absolute():
            suite_path = exp.base_dir / suite_path
        suite, tasks = load_suite(suite_path)
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
                raise SpecError(f"unknown variant {vid!r}; available: {', '.join(sorted(catalogue))}")
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

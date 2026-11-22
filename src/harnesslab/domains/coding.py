"""The ``coding`` domain adapter: coding agents through the existing run pipeline, unchanged.

Every cell is one ordinary Harness Lab run. :meth:`ExperimentService.execute_run` creates a git
worktree from the task's fixture snapshot, runs the setup commands and the harness runner,
captures the diff, runs the hidden-test verifier (:class:`CommandVerifier`) after the agent has
exited, computes the run's metrics and stores everything in the Harness Lab database, exactly as
``harnesslab run`` does. This module only maps the domain contract onto those objects (ADR
2026-11-22, "How the coding adapter maps onto the contract").

Options (unknown options are an error):

- ``suite`` (required): a ``suite.yaml`` path, relative to the study directory, or the name of a
  bundled suite such as ``demo``. The suite's ``plugins`` are imported, as ``harnesslab run``
  does, so that runners they register exist.
- ``home``: the Harness Lab home (database, fixture snapshots, worktrees, artifacts), relative to
  the study directory; default as for every ``harnesslab`` command: ``$HARNESSLAB_HOME``, else
  ``./.harnesslab``. ``$HARNESSLAB_DB_URL`` is honoured.
- ``pricing``: a ``pricing.yaml`` for cost estimates, relative to the study directory; default
  ``pricing.yaml`` in the home directory or the working directory, as ``harnesslab run`` does.
- ``knobs`` (required), in one of two forms:

  - ``{variant: suite}``: one knob, ``variant``, whose levels are the ids of the suite's variants.
    With ``variants: [ids]`` the levels are those ids, in that order, resolved as
    ``harnesslab run --variants`` resolves them (the suite's variants, then the built-in ones).
  - a mapping knob -> levels in the sweep factor format, merged over ``base_variant`` (a variant
    mapping that names a ``runner``) by :func:`expand_sweep`, as ``harnesslab sweep`` merges
    them: a list of values sets the variant key named like the knob (the knob's levels are the
    values), and a mapping level name -> overrides merges those overrides (the levels are the
    names). Knobs merge in the order given; a later knob wins on a key both set. A
    configuration's variant id is the sweep's, e.g. ``behavior=solve|cost=cheap``.

- ``baseline`` (required): knob -> level, a point of the search space.

How the contract maps onto the pipeline:

``load_tasks``
    The suite's tasks by id. The content hash is the experiment service's ``task_hash``: the hash
    of the task's spec hash and the base commit of the fixture snapshot. The snapshot is taken
    once per task and reused by every run of that task.
``run``
    One Harness Lab experiment per (study, phase), named ``study:<study>:<phase>``, is created on
    first use; task and variant rows are added the first time each is used, as
    :meth:`ExperimentService.run_experiment` adds them (suite defaults applied, harness bundles
    snapshotted). Then :meth:`ExperimentService.execute_run` runs the cell with the cell's
    repetition and removes its worktree. The attempt's ``payload`` is the run's
    :class:`RunOutcome`; ``output`` identifies the run and repeats the verifier's outcome and
    score.
``verify``
    The verdict the hidden-test verifier recorded during the run: pass, fail, or (not verified)
    unverifiable. Nothing is re-run (the worktree no longer exists); the verdict stays
    independent of the agent because the hidden tests are injected after the agent exits, in an
    environment without provider credentials.
``metrics``
    The run's metrics: wall time, tokens, LLM calls, tool calls, files changed, verified score,
    cost (reported, else estimated from the pricing table, else ``None``) and safety counts.
``close``
    Marks this adapter's experiments finished: ``completed``, ``interrupted`` when a run was
    cancelled, ``failed`` when the pipeline itself raised.

``paid`` is true when a configuration can use any runner but ``fake`` (for factors: the base
runner and every runner a level sets). ``provenance()`` records the Harness Lab version and
commit, the suite and the SHA-256 of its file, and the pricing table's version.
``audit_split()`` reports held-out tasks missing from the suite, repeating a dev task's prompt,
or twinned with a dev task by ``fault_of``.

What the pipeline does not use: ``RunContext.cost_limit_usd`` (the pipeline cannot stop a harness
at a cost; a Claude Code variant can cap itself with ``max_budget_usd``, and the core stops the
phase when an attempt reports more than its cap), ``seed``, ``attempt_dir`` and
``keep_artifacts`` (a run's diff, trace and verifier output stay in the Harness Lab home under
``artifacts/<experiment_id>/<run_id>``). A runner that reports no cost, without a pricing table
entry for its model, is charged nothing (``details.cost_source`` is ``none``).
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import harnesslab
from harnesslab.config import Settings
from harnesslab.core.ids import hash_value
from harnesslab.core.models import (
    EnvironmentSpec,
    ExperimentSpec,
    Outcome,
    RunMetrics,
    RunStatus,
    SweepSpec,
    TaskSpec,
    VariantSpec,
)
from harnesslab.core.pricing import PricingTable, find_pricing_table
from harnesslab.execution.fixture import RepoSnapshot
from harnesslab.execution.git import git_version, harnesslab_source_commit
from harnesslab.execution.sandbox import LocalWorktreeSandbox
from harnesslab.experiments.service import ExperimentService, RunOutcome
from harnesslab.experiments.spec import (
    load_suite,
    resolve_suite_reference,
    resolve_variant_harness,
    resolve_variants,
)
from harnesslab.experiments.sweep import expand_sweep
from harnesslab.harness.bundle import HarnessBundle
from harnesslab.runners.base import load_plugins
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository
from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapterBase,
    Knob,
    Metrics,
    RunContext,
    SearchSpace,
    Task,
    Usage,
    Verdict,
    VerdictStatus,
    VerifyContext,
    canonical_json,
)

NAME = "coding"
VERSION = "1"
CHECKER = "harnesslab CommandVerifier (hidden tests)"
VARIANT_KNOB = "variant"
# Runners that need nothing but this machine (the CLI's list); every other one starts an agent
# CLI or a plugin's harness that may use a paid API.
OFFLINE_RUNNERS = frozenset({"fake"})
OPTIONS = frozenset({"suite", "home", "pricing", "knobs", "variants", "base_variant", "baseline"})

# Every other run status (crashed, blocked, interrupted, ...) is an error.
ATTEMPT_STATUS: dict[RunStatus, AttemptStatus] = {
    RunStatus.COMPLETED: AttemptStatus.COMPLETED,
    RunStatus.TIMEOUT: AttemptStatus.TIMEOUT,
    RunStatus.UNAVAILABLE: AttemptStatus.UNAVAILABLE,
}
VERDICT_STATUS: dict[Outcome, VerdictStatus] = {
    Outcome.PASS: VerdictStatus.PASS,
    Outcome.FAIL: VerdictStatus.FAIL,
    Outcome.NOT_VERIFIED: VerdictStatus.UNVERIFIABLE,
}


@dataclass(frozen=True)
class _PreparedTask:
    """A suite task with the fixture snapshot every run of it starts from."""

    spec: TaskSpec
    snapshot: RepoSnapshot
    task_hash: str


@dataclass
class _RegisteredVariant:
    row_id: str
    variant: VariantSpec  # this experiment's copy, harness hash filled in
    config_hash: str


@dataclass
class _Experiment:
    """One Harness Lab experiment holding the runs of one (study, phase)."""

    id: str
    tasks: dict[str, str] = field(default_factory=dict)  # task id -> task row id
    variants: dict[str, _RegisteredVariant] = field(default_factory=dict)  # by variant id
    interrupted: bool = False
    failed: bool = False


def _cost(metrics: RunMetrics) -> tuple[float | None, str]:
    """A run's cost and where it comes from: reported by the harness, else estimated."""
    if metrics.reported_cost_usd is not None:
        return metrics.reported_cost_usd, "reported"
    if metrics.estimated_cost_usd is not None:
        return metrics.estimated_cost_usd, "estimated"
    return None, "none"


def _matching_level(knob: Knob, value: Any) -> Any:
    key = canonical_json(value)
    for level in knob.levels:
        if canonical_json(level) == key:
            return level
    raise ValueError(f"{value!r} is not a level of knob {knob.name!r}")


def _reason(outcome: RunOutcome) -> str:
    metrics = outcome.metrics
    parts = [f"verifier outcome {outcome.outcome.value}"]
    if metrics.verifier_exit_code is not None:
        parts.append(f"verification exit code {metrics.verifier_exit_code}")
    if metrics.verified_score is not None:
        parts.append(f"score {metrics.verified_score:g}")
    text = ", ".join(parts)
    if outcome.status != RunStatus.COMPLETED or outcome.error:
        text += f"; run {outcome.status.value}"
        if outcome.error:
            text += f": {outcome.error}"
    return text


class CodingAdapter(DomainAdapterBase):
    """Coding tasks of one suite, run by the existing pipeline; see the module docstring."""

    name = NAME
    version = VERSION

    def __init__(self, options: dict[str, Any], study_dir: Path) -> None:
        if not isinstance(options, dict):
            raise TypeError("coding adapter options must be a mapping")
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ValueError(
                f"unknown coding adapter option(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(OPTIONS))})"
            )
        self.study_dir = Path(study_dir)
        suite_ref = options.get("suite")
        if not isinstance(suite_ref, str | Path) or not str(suite_ref).strip():
            raise ValueError("option suite is required: a suite.yaml path or a bundled suite name")
        self.suite_path = resolve_suite_reference(str(suite_ref), self.study_dir)
        self.suite, tasks = load_suite(self.suite_path)
        load_plugins(self.suite.plugins)
        self._tasks: dict[str, TaskSpec] = {t.id: t for t in tasks}
        self._positions = {t.id: i for i, t in enumerate(tasks)}
        self.settings = Settings.from_env(self._path_option(options, "home"))
        self.pricing: PricingTable | None = find_pricing_table(
            self._path_option(options, "pricing"), [self.settings.home, Path.cwd()]
        )

        knobs = options.get("knobs")
        if not isinstance(knobs, dict) or not knobs:
            raise ValueError(
                "option knobs is required: {variant: suite} or a mapping knob -> levels"
            )
        self._suite_variants: dict[str, VariantSpec] = {}
        self._base_variant: dict[str, Any] = {}
        self._factors: dict[str, Any] = {}
        if knobs.get(VARIANT_KNOB) == "suite":
            if len(knobs) > 1:
                raise ValueError("knobs {variant: suite} cannot be combined with other knobs")
            if "base_variant" in options:
                raise ValueError("base_variant is used only with knobs in the sweep factor form")
            self.knob_form = "variant"
            self._suite_variants = self._resolve_suite_variants(options.get("variants"))
            space = SearchSpace(
                knobs=(
                    Knob(
                        name=VARIANT_KNOB,
                        levels=tuple(self._suite_variants),
                        description=f"a variant of suite {self.suite.name}",
                    ),
                )
            )
            runners = {v.runner for v in self._suite_variants.values()}
        else:
            if "variants" in options:
                raise ValueError("variants is used only with knobs {variant: suite}")
            self.knob_form = "factors"
            space, runners = self._factor_space(knobs, options.get("base_variant"))
        self._space = space
        # Every runner a configuration can use (for factors: the base runner and every runner a
        # level sets, which may include a base runner that no configuration ends up using).
        self.runners = sorted(runners)
        self.paid = any(r not in OFFLINE_RUNNERS for r in runners)

        baseline = options.get("baseline")
        if not isinstance(baseline, dict) or not baseline:
            raise ValueError("option baseline is required: knob -> level, a point of the space")
        self._baseline = Config(values=dict(baseline), label="baseline")
        self._space.validate_config(self._baseline)

        self._lock = threading.Lock()  # guards the snapshot cache and the database handle
        self._snapshots = LocalWorktreeSandbox(self.settings)
        self._prepared: dict[str, _PreparedTask] = {}
        self._variant_cache: dict[str, VariantSpec] = {}
        self._db: Database | None = None
        self._environment: EnvironmentSpec | None = None
        # The worktree manager's locks belong to one event loop: one service per loop.
        self._loop_service: tuple[asyncio.AbstractEventLoop, ExperimentService] | None = None
        self._experiments: dict[tuple[str, str], _Experiment] = {}

        # Build the baseline variant and, for every level, the baseline with that one level
        # changed, so that a malformed variant or a missing harness bundle fails here.
        for knob in self._space.knobs:
            for level in knob.levels:
                self._variant_for(Config(values={**self._baseline.values, knob.name: level}))

    # -- options -----------------------------------------------------------------
    def _path_option(self, options: dict[str, Any], key: str) -> Path | None:
        value = options.get(key)
        if value is None:
            return None
        if not isinstance(value, str | Path) or not str(value).strip():
            raise ValueError(f"option {key} must be a path")
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.study_dir / path).resolve()

    def _resolve_suite_variants(self, requested: Any) -> dict[str, VariantSpec]:
        if requested is None:
            chosen = list(self.suite.variants)
            if not chosen:
                raise ValueError(
                    f"suite {self.suite.name} defines no variants: name the variants to compare "
                    "with the variants option"
                )
        else:
            if (
                not isinstance(requested, list)
                or not requested
                or not all(isinstance(v, str) for v in requested)
            ):
                raise ValueError("option variants must be a non-empty list of variant ids")
            experiment = ExperimentSpec(name=self.suite.name, suite=str(self.suite_path))
            chosen = resolve_variants(list(requested), experiment, self.suite)
        ids = [v.id for v in chosen]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ValueError(f"variants are listed more than once: {', '.join(repeated)}")
        return {v.id: v for v in chosen}

    def _factor_space(
        self, knobs: dict[str, Any], base_variant: Any
    ) -> tuple[SearchSpace, set[str]]:
        if not isinstance(base_variant, dict):
            raise ValueError(
                "knobs in the sweep factor form need base_variant: a variant mapping with a runner"
            )
        # The sweep's own validation: base_variant names a runner, every factor has levels.
        template = SweepSpec(
            name=f"{NAME}:{self.suite.name}",
            suite=str(self.suite_path),
            base_variant=base_variant,
            factors=knobs,
        )
        self._base_variant = template.base_variant
        self._factors = dict(template.factors)
        space_knobs: list[Knob] = []
        runners = {str(self._base_variant["runner"])}
        for name, levels in self._factors.items():
            if isinstance(levels, dict):
                values: tuple[Any, ...] = tuple(levels)
                overrides = [dict(o or {}) for o in levels.values()]
                description = "level name -> variant overrides"
            else:
                values = tuple(levels)
                names = [str(v) for v in values]
                clashing = sorted({n for n in names if names.count(n) > 1})
                if clashing:
                    raise ValueError(
                        f"knob {name!r}: several levels would name the variant "
                        f"{', '.join(clashing)}"
                    )
                overrides = [{name: v} for v in values]
                description = f"sets the variant key {name!r}"
            runners |= {str(o["runner"]) for o in overrides if "runner" in o}
            space_knobs.append(Knob(name=name, levels=values, description=description))
        return SearchSpace(knobs=tuple(space_knobs)), runners

    # -- the contract -------------------------------------------------------------
    def search_space(self) -> SearchSpace:
        return self._space

    def baseline_config(self) -> Config:
        return Config(values=dict(self._baseline.values), label="baseline")

    def load_tasks(self, ids: Sequence[str]) -> list[Task]:
        unknown = [i for i in ids if i not in self._tasks]
        if unknown:
            raise ValueError(
                f"unknown task id(s) in suite {self.suite.name}: {', '.join(unknown)} "
                f"(tasks: {', '.join(self._tasks)})"
            )
        return [self._task(self._prepare(i)) for i in ids]

    async def run(self, task: Task, config: Config, ctx: RunContext) -> Attempt:
        variant = self._variant_for(config)
        if task.id not in self._tasks:
            raise ValueError(f"task {task.id!r} is not in suite {self.suite.name}")
        prepared = await asyncio.to_thread(self._prepare, task.id)
        if prepared.task_hash != task.content_hash:
            raise ValueError(
                f"task {task.id}: content hash {task.content_hash} does not match its fixture "
                f"snapshot ({prepared.task_hash}); the task changed since it was loaded"
            )
        service = await self._service()
        # No await from here to execute_run: rows are added once even with concurrent cells.
        experiment = self._experiment(service, ctx.study, ctx.phase)
        task_row_id = self._task_row(service, experiment, prepared)
        registered = self._variant_row(service, experiment, variant, config)
        try:
            outcome = await service.execute_run(
                exp_id=experiment.id,
                task=prepared.spec,
                variant=registered.variant,
                repetition=ctx.repetition,
                task_row_id=task_row_id,
                task_hash=prepared.task_hash,
                variant_row_id=registered.row_id,
                environment=self._require_environment(),
                keep_worktree=False,
                progress=None,
                snapshot=prepared.snapshot,
            )
        except asyncio.CancelledError:
            experiment.interrupted = True
            raise
        except Exception:
            experiment.failed = True
            raise
        metrics = outcome.metrics
        cost, cost_source = _cost(metrics)
        return Attempt(
            task_id=task.id,
            config_hash=config.config_hash,
            repetition=ctx.repetition,
            status=ATTEMPT_STATUS.get(outcome.status, AttemptStatus.ERROR),
            output={
                "run_id": outcome.run_id,
                "experiment_id": experiment.id,
                "outcome": outcome.outcome.value,
                "verified_score": metrics.verified_score,
            },
            usage=Usage(
                cost_usd=cost if cost is not None else 0.0,
                input_tokens=metrics.input_tokens or 0,
                output_tokens=metrics.output_tokens or 0,
                cache_read_tokens=metrics.cached_input_tokens or 0,
                cache_write_tokens=metrics.cache_write_tokens or 0,
                llm_calls=metrics.llm_calls or 0,
            ),
            wall_time_s=metrics.wall_time_seconds,
            error=outcome.error,
            details={
                "run_status": outcome.status.value,
                "variant": registered.variant.id,
                "runner": registered.variant.runner,
                "model": registered.variant.model,
                "harness_hash": registered.variant.harness_hash,
                "cost_source": cost_source,
            },
            payload=outcome,
        )

    async def verify(self, task: Task, attempt: Attempt, ctx: VerifyContext) -> Verdict:
        outcome = self._outcome(attempt)
        metrics = outcome.metrics
        return Verdict(
            status=VERDICT_STATUS[outcome.outcome],
            reason=_reason(outcome),
            checker=CHECKER,
            details={
                "run_id": outcome.run_id,
                "run_status": outcome.status.value,
                "outcome": outcome.outcome.value,
                "verified_pass": metrics.verified_pass,
                "verified_score": metrics.verified_score,
                "verifier_exit_code": metrics.verifier_exit_code,
                "visible_pass": metrics.visible_pass,
            },
            duration_s=metrics.verifier_wall_time_seconds,
        )

    def metrics(self, task: Task, attempt: Attempt, verdict: Verdict) -> Metrics:
        m = self._outcome(attempt).metrics
        cost, _ = _cost(m)
        return {
            "wall_time_s": m.wall_time_seconds,
            "agent_wall_time_s": m.agent_wall_time_seconds,
            "verifier_wall_time_s": m.verifier_wall_time_seconds,
            "input_tokens": m.input_tokens,
            "cached_input_tokens": m.cached_input_tokens,
            "cache_write_tokens": m.cache_write_tokens,
            "output_tokens": m.output_tokens,
            "total_tokens": m.total_tokens,
            "llm_calls": m.llm_calls,
            "tool_calls": m.tool_calls,
            "shell_commands": m.shell_commands,
            "files_changed": m.files_changed,
            "lines_added": m.lines_added,
            "lines_deleted": m.lines_deleted,
            "verified_score": m.verified_score,
            "cost_usd": cost,
            "reported_cost_usd": m.reported_cost_usd,
            "estimated_cost_usd": m.estimated_cost_usd,
            "risky_actions": m.risky_actions,
            "safety_violations": m.safety_violations,
            "improve_ratio": m.improve_ratio,
        }

    # -- optional parts --------------------------------------------------------------
    def provenance(self) -> dict[str, Any]:
        return {
            "harnesslab_version": harnesslab.__version__,
            "harnesslab_commit": harnesslab_source_commit(
                Path(harnesslab.__file__).resolve().parent
            ),
            "suite": self.suite.name,
            "suite_sha256": hashlib.sha256(self.suite_path.read_bytes()).hexdigest(),
            "knob_form": self.knob_form,
            "runners": list(self.runners),
            "pricing_version": self.pricing.version if self.pricing else None,
        }

    def audit_split(self, dev: list[Task], held_out_ids: Sequence[str]) -> list[str]:
        """Held-out tasks that are not in the suite, repeat a dev task's prompt, or are the
        fault-free or faulty twin (``fault_of``) of a dev task."""
        problems: list[str] = []
        missing = [i for i in held_out_ids if i not in self._tasks]
        if missing:
            problems.append(
                f"held-out task(s) not in suite {self.suite.name}: {', '.join(missing)}"
            )
        dev_specs = {t.id: self._tasks[t.id] for t in dev if t.id in self._tasks}
        dev_prompts = {spec.prompt_hash(): spec.id for spec in dev_specs.values()}
        held_out = set(held_out_ids)
        for task_id in held_out_ids:
            spec = self._tasks.get(task_id)
            if spec is None or task_id in dev_specs:
                continue
            twin = dev_prompts.get(spec.prompt_hash())
            if twin is not None:
                problems.append(f"held-out task {task_id} has the prompt of dev task {twin}")
            if spec.fault_of in dev_specs:
                problems.append(
                    f"held-out task {task_id} is the faulty twin of dev task {spec.fault_of}"
                )
        for spec in dev_specs.values():
            if spec.fault_of is not None and spec.fault_of in held_out:
                problems.append(
                    f"dev task {spec.id} is the faulty twin of held-out task {spec.fault_of}"
                )
        return problems

    async def close(self) -> None:
        """Mark this adapter's experiments finished and release the database."""
        experiments, self._experiments = self._experiments, {}
        with self._lock:
            db, self._db = self._db, None
            self._loop_service = None
        if db is None:
            return
        try:
            repo = Repository(db, self.settings.home)
            for experiment in experiments.values():
                if experiment.interrupted:
                    repo.mark_stale_runs_interrupted(experiment.id)
                    status = "interrupted"
                else:
                    status = "failed" if experiment.failed else "completed"
                repo.finish_experiment(experiment.id, status)
        finally:
            db.dispose()

    # -- tasks -----------------------------------------------------------------------
    def _prepare(self, task_id: str) -> _PreparedTask:
        """The task and its fixture snapshot, taken once (blocking: git work)."""
        with self._lock:
            prepared = self._prepared.get(task_id)
            if prepared is None:
                spec = self._tasks[task_id]
                snapshot = self._snapshots.snapshot(spec)
                task_hash = hash_value(
                    {"spec": spec.spec_hash(), "base_commit": snapshot.base_commit}
                )
                prepared = _PreparedTask(spec=spec, snapshot=snapshot, task_hash=task_hash)
                self._prepared[task_id] = prepared
            return prepared

    def _task(self, prepared: _PreparedTask) -> Task:
        spec = prepared.spec
        return Task(
            id=spec.id,
            content_hash=prepared.task_hash,
            data={
                "suite": self.suite.name,
                "name": spec.name,
                "version": spec.version,
                "spec_hash": spec.spec_hash(),
                "prompt_hash": spec.prompt_hash(),
                "base_commit": prepared.snapshot.base_commit,
            },
            tags=tuple(spec.tags),
        )

    # -- configurations --------------------------------------------------------------
    def _variant_for(self, config: Config) -> VariantSpec:
        """The variant a configuration runs, with the suite's defaults filled in."""
        key = config.config_hash
        variant = self._variant_cache.get(key)
        if variant is not None:
            return variant
        self._space.validate_config(config)
        if self.knob_form == "variant":
            variant = self._suite_variants[config.values[VARIANT_KNOB]]
        else:
            factors: dict[str, Any] = {}
            for knob in self._space.knobs:
                level = _matching_level(knob, config.values[knob.name])
                spec_levels = self._factors[knob.name]
                factors[knob.name] = (
                    {level: spec_levels[level]} if isinstance(spec_levels, dict) else [level]
                )
            # The one configuration of a sweep whose factors each have this one level.
            (variant,) = expand_sweep(
                SweepSpec(
                    name=f"{NAME}:{self.suite.name}",
                    suite=str(self.suite_path),
                    base_variant=self._base_variant,
                    factors=factors,
                )
            )
            resolve_variant_harness(variant, self.study_dir)
        variant = variant.with_defaults(self.suite.defaults_for(variant.runner))
        self._variant_cache[key] = variant
        return variant

    # -- the Harness Lab database ----------------------------------------------------
    async def _service(self) -> ExperimentService:
        loop = asyncio.get_running_loop()
        current = self._loop_service
        if current is not None and current[0] is loop:
            return current[1]
        return await asyncio.to_thread(self._service_for, loop)

    def _service_for(self, loop: asyncio.AbstractEventLoop) -> ExperimentService:
        """The service of ``loop``, built once; the database is opened on first use (blocking:
        git and SQLite)."""
        with self._lock:
            current = self._loop_service
            if current is not None and current[0] is loop:  # another cell built it meanwhile
                return current[1]
            if self._db is None:
                self.settings.ensure_dirs()
                db = Database(self.settings.resolved_database_url)
                db.create_all()
                # As every harnesslab command does on opening the database.
                Repository(db, self.settings.home).reconcile_orphaned_experiments()
                self._db = db
            if self._environment is None:
                self._environment = EnvironmentSpec.detect(
                    sandbox=LocalWorktreeSandbox.kind, git_version=git_version()
                )
            service = ExperimentService(
                self.settings,
                self._db,
                pricing=self.pricing,
                sandbox=LocalWorktreeSandbox(self.settings),
            )
            self._loop_service = (loop, service)
            return service

    def _require_environment(self) -> EnvironmentSpec:
        if self._environment is None:  # set by _service_for before any run
            raise RuntimeError("the coding adapter's environment was not detected")
        return self._environment

    def _experiment(self, service: ExperimentService, study: str, phase: str) -> _Experiment:
        experiment = self._experiments.get((study, phase))
        if experiment is None:
            spec = ExperimentSpec(
                name=f"study:{study}:{phase}",
                suite=str(self.suite_path),
                description=(
                    f"Phase {phase} of study {study}: cells run by the coding domain adapter "
                    "(the study sets the configurations and repetitions)."
                ),
            )
            exp_id = service.repo.create_experiment(
                spec,
                self.suite,
                environment=self._require_environment(),
                harnesslab_version=service.harnesslab_version,
                harnesslab_commit=service.harnesslab_commit,
            )
            experiment = _Experiment(id=exp_id)
            self._experiments[(study, phase)] = experiment
        return experiment

    def _task_row(
        self, service: ExperimentService, experiment: _Experiment, prepared: _PreparedTask
    ) -> str:
        row_id = experiment.tasks.get(prepared.spec.id)
        if row_id is None:
            row_id = service.repo.add_task(
                experiment.id,
                prepared.spec,
                base_commit=prepared.snapshot.base_commit,
                task_hash=prepared.task_hash,
                position=self._positions[prepared.spec.id],
            )
            experiment.tasks[prepared.spec.id] = row_id
        return row_id

    def _variant_row(
        self,
        service: ExperimentService,
        experiment: _Experiment,
        variant: VariantSpec,
        config: Config,
    ) -> _RegisteredVariant:
        """Add the variant to the experiment as ``run_experiment`` does, the first time only."""
        registered = experiment.variants.get(variant.id)
        if registered is not None:
            if registered.config_hash != config.config_hash:
                raise ValueError(
                    f"configurations {registered.config_hash[:12]} and {config.config_hash[:12]} "
                    f"both map to the variant id {variant.id!r}"
                )
            return registered
        variant = variant.model_copy(deep=True)
        harness_json = None
        if variant.harness_dir is not None:
            bundle = HarnessBundle.load(variant.harness_dir)
            variant.harness_hash = bundle.hash
            snapshot_dir = self.settings.artifacts_dir / experiment.id / "harness" / bundle.hash
            if not snapshot_dir.exists():
                bundle.write_to(snapshot_dir)
            harness_json = {
                "name": bundle.name,
                "description": bundle.description,
                "source": str(variant.harness_dir),
                "files": bundle.file_summary(),
            }
        row_id = service.repo.add_variant(
            experiment.id, variant, len(experiment.variants), harness_json=harness_json
        )
        registered = _RegisteredVariant(
            row_id=row_id, variant=variant, config_hash=config.config_hash
        )
        experiment.variants[variant.id] = registered
        return registered

    @staticmethod
    def _outcome(attempt: Attempt) -> RunOutcome:
        outcome = attempt.payload
        if not isinstance(outcome, RunOutcome):
            raise TypeError(
                "the coding adapter needs the attempt returned by its run() in this process: "
                "attempt.payload must be the run's RunOutcome"
            )
        if outcome.run_id != attempt.output.get("run_id"):
            raise ValueError(
                f"attempt payload is run {outcome.run_id}, the attempt names run "
                f"{attempt.output.get('run_id')}"
            )
        return outcome


def create_adapter(options: dict[str, Any], study_dir: Path) -> CodingAdapter:
    """The registry's factory for ``coding``."""
    return CodingAdapter(options, study_dir)

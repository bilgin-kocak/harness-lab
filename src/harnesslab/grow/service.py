"""The Growing Harness loop.

Every evaluation is an ordinary :class:`ExperimentService` experiment tagged with the session
and its role (``baseline_gate``, ``baseline_train``, ``window``, ``gate``, ``final``), so the
verifier, traces, redaction and dashboard all apply unchanged.  Design spec, section 3::

    v0 = initial bundle
    baseline_gate:  gate x v0        -> gate pass rate
    baseline_train: train x v0       -> failure pool
    iterate: window <- pool; optimizer -> candidate; lint;
             window x candidate (>= Q fixed?); gate x candidate (no regression?);
             accept or reject (rollback is implicit: "current" only moves on accept)
    final:   final x {v0, current}

Session state is persisted after every step so ``resume`` continues where it stopped.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, SuiteSpec, TaskSpec, VariantSpec
from harnesslab.core.pricing import PricingTable
from harnesslab.experiments.service import ExperimentService, RunOutcome
from harnesslab.experiments.spec import load_suite, select_tasks
from harnesslab.grow.optimizers.base import Proposal, create_optimizer
from harnesslab.grow.spec import GrowBudget, GrowSpec, ResolvedSplit
from harnesslab.grow.view import build_context, build_failure_case
from harnesslab.harness.bundle import BundleError, HarnessBundle
from harnesslab.harness.lint import EditConstraints, SuiteSecrets, lint_candidate
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository, utcnow

MAX_CONSECUTIVE_OPTIMIZER_FAILURES = 3
GATE_ROLES = ("baseline_gate", "gate")
VARIANT_RESERVED = ("id", "runner", "model", "harness", "gate_overrides")


class GrowError(RuntimeError):
    pass


class GrowState(BaseModel):
    phase: str = "baseline_gate"
    iteration: int = 0
    pool: list[str] = Field(default_factory=list)
    attempts: dict[str, int] = Field(default_factory=dict)
    retired: list[str] = Field(default_factory=list)
    initial_version_id: str | None = None
    current_version_id: str | None = None
    current_gate_pass_rate: float | None = None
    next_version_number: int = 1
    pending_version_id: str | None = None
    consecutive_optimizer_failures: int = 0
    runs_started: int = 0
    deployed_cost_usd: float = 0.0
    optimizer_cost_usd: float = 0.0
    previous_rejections: list[str] = Field(default_factory=list)
    versions_accepted: int = 0
    versions_rejected: int = 0
    versions_invalid: int = 0
    notes: list[str] = Field(default_factory=list)
    split: dict[str, list[str]] = Field(default_factory=dict)


class GrowProgress(BaseModel):
    kind: str  # phase | version | note
    message: str
    version_number: int | None = None


ProgressCallback = Callable[[GrowProgress], None]


class GrowOutcome(BaseModel):
    session_id: str
    status: str
    iterations: int
    initial_version_id: str | None
    current_version_id: str | None
    versions_accepted: int
    versions_rejected: int
    versions_invalid: int
    notes: list[str] = Field(default_factory=list)


class GrowService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        pricing: PricingTable | None = None,
        experiment_service: ExperimentService | None = None,
        optimizer_factory: Callable[..., Any] = create_optimizer,
    ) -> None:
        self.settings = settings
        self.db = db
        self.repo = Repository(db, settings.home)
        self.experiments = experiment_service or ExperimentService(settings, db, pricing=pricing)
        self.optimizer_factory = optimizer_factory

    def session_dir(self, session_id: str) -> Path:
        return self.settings.home / "grow" / session_id

    # ------------------------------------------------------------------
    async def run(
        self,
        spec: GrowSpec,
        suite: SuiteSpec,
        tasks: list[TaskSpec],
        split: ResolvedSplit,
        *,
        progress: ProgressCallback | None = None,
    ) -> GrowOutcome:
        self.settings.ensure_dirs()
        spec_json = spec.model_dump(mode="json")
        spec_json["source_path"] = str(spec.source_path) if spec.source_path else None
        spec_json["harness_dir"] = str(spec.harness_dir) if spec.harness_dir else None
        session_id = self.repo.create_grow_session(
            name=spec.name,
            suite_name=suite.name,
            suite_path=str(suite.source_path) if suite.source_path else None,
            spec_json=spec_json,
            harnesslab_version=self.experiments.harnesslab_version,
            harnesslab_commit=self.experiments.harnesslab_commit,
        )
        state = GrowState(split=split.model_dump())
        v0_dir = self.session_dir(session_id) / "v0" / "bundle"
        if spec.harness_dir is not None:
            bundle = HarnessBundle.load(spec.harness_dir)
        else:
            bundle = HarnessBundle(path=v0_dir, files={})
        bundle.write_to(v0_dir)
        v0 = self.repo.create_harness_version(
            session_id=session_id,
            number=0,
            parent_id=None,
            harness_hash=bundle.hash,
            bundle_path=self._relative(v0_dir),
            status="initial",
        )
        state.initial_version_id = v0
        state.current_version_id = v0
        self.repo.update_grow_session(
            session_id,
            initial_version_id=v0,
            current_version_id=v0,
            state_json=state.model_dump(mode="json"),
        )
        return await self._drive(session_id, spec, suite, tasks, split, state, progress)

    async def resume(
        self, session_ref: str, *, progress: ProgressCallback | None = None
    ) -> GrowOutcome:
        row = self.repo.find_grow_session(session_ref)
        if row is None:
            raise GrowError(f"grow session not found: {session_ref}")
        if row.status == "completed" or row.phase == "done":
            raise GrowError(f"session {row.id} is not resumable (status {row.status})")
        spec_json = dict(row.spec_json)
        source = spec_json.pop("source_path", None)
        harness_dir = spec_json.pop("harness_dir", None)
        spec = GrowSpec(**spec_json)
        spec.source_path = Path(source) if source else None
        spec.harness_dir = Path(harness_dir) if harness_dir else None
        if not row.suite_path:
            raise GrowError(f"session {row.id} has no suite path recorded")
        suite, all_tasks = load_suite(Path(row.suite_path))
        state = GrowState(**(row.state_json or {}))
        split = ResolvedSplit(**state.split)
        tasks = select_tasks(all_tasks, split.train + split.gate + split.final)
        if state.pending_version_id:
            self.repo.update_harness_version(
                state.pending_version_id,
                status="discarded",
                reason="interrupted before evaluation finished",
                finished_at=utcnow(),
            )
            state.pending_version_id = None
        self.repo.update_grow_session(row.id, status="running", finished_at=None)
        return await self._drive(row.id, spec, suite, tasks, split, state, progress)

    # ------------------------------------------------------------------
    async def _drive(
        self,
        session_id: str,
        spec: GrowSpec,
        suite: SuiteSpec,
        tasks: list[TaskSpec],
        split: ResolvedSplit,
        state: GrowState,
        progress: ProgressCallback | None,
    ) -> GrowOutcome:
        by_id = {t.id: t for t in tasks}
        secrets = SuiteSecrets.from_tasks(tasks)
        gate_tasks = [by_id[t] for t in split.gate]
        train_tasks = [by_id[t] for t in split.train]
        budget = spec.budget

        def save() -> None:
            self.repo.update_grow_session(
                session_id,
                phase=state.phase,
                state_json=state.model_dump(mode="json"),
                iterations=state.iteration,
                current_version_id=state.current_version_id,
            )

        def report(kind: str, message: str, number: int | None = None) -> None:
            if progress:
                progress(GrowProgress(kind=kind, message=message, version_number=number))

        status = "completed"
        try:
            if state.phase == "baseline_gate":
                report("phase", "baseline: gate set on the initial harness")
                if not self._over_budget(state, budget, len(gate_tasks) * spec.repetitions):
                    stats = await self._evaluate(
                        session_id,
                        spec,
                        suite,
                        state,
                        state.current_version_id,
                        gate_tasks,
                        "baseline_gate",
                    )
                    self.repo.update_harness_version(state.current_version_id, **stats.fields())
                    state.current_gate_pass_rate = stats.pass_rate
                    state.phase = "baseline_train"
                else:
                    state.phase = "done"
                save()
            if state.phase == "baseline_train":
                report("phase", "baseline: train set on the initial harness")
                if not self._over_budget(state, budget, len(train_tasks) * spec.repetitions):
                    runs, _ = await self._run_role(
                        session_id,
                        spec,
                        suite,
                        state,
                        state.current_version_id,
                        train_tasks,
                        "baseline_train",
                    )
                    state.pool = [t.id for t in train_tasks if not self._all_passed(runs, t.id)]
                    state.attempts = {t: 0 for t in state.pool}
                    state.phase = "iterating"
                else:
                    state.phase = "done"
                save()
            while state.phase == "iterating":
                if state.iteration >= spec.max_iterations:
                    state.notes.append(f"stopped: max_iterations {spec.max_iterations} reached")
                    break
                if not state.pool:
                    state.notes.append("stopped: failure pool empty")
                    break
                window = self._window(state, spec, split)
                planned = (len(window) + len(gate_tasks)) * spec.repetitions
                if self._over_budget(state, budget, planned):
                    break
                current = self.repo.get_harness_version(state.current_version_id)
                current_bundle = HarnessBundle.load(self.settings.home / current.bundle_path)
                state.iteration += 1
                number = state.next_version_number
                state.next_version_number += 1
                vdir = self.session_dir(session_id) / f"v{number}"
                bundle_dir = vdir / "bundle"
                vdir.mkdir(parents=True, exist_ok=True)
                report("phase", f"iteration {state.iteration}: window {', '.join(window)}", number)
                cases = [
                    build_failure_case(
                        self.repo,
                        self.repo.latest_run_for_task(session_id, t),
                        by_id[t],
                        secrets,
                        state.attempts.get(t, 0),
                    )
                    for t in window
                ]
                constraints = EditConstraints(max_files=spec.optimizer.max_files)
                context = build_context(
                    session_name=spec.name,
                    iteration=state.iteration,
                    runner=spec.base_variant["runner"],
                    model=spec.base_variant.get("model"),
                    bundle=current_bundle,
                    cases=cases,
                    constraints=constraints,
                    previous_rejections=state.previous_rejections,
                    suite_description=suite.description,
                )
                (vdir / "context.json").write_text(context.model_dump_json(indent=2))
                if self._optimizer_over_budget(state, budget):
                    break
                optimizer = self.optimizer_factory(
                    spec.optimizer.kind, spec.optimizer.options, artifacts_dir=vdir
                )
                try:
                    proposal = await optimizer.propose(context)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = f"optimizer error: {type(exc).__name__}: {exc}"
                    self._record_invalid(
                        session_id, state, number, current, vdir, reason, window, spec, None
                    )
                    report("version", f"v{number} invalid: {reason}", number)
                    save()
                    if state.consecutive_optimizer_failures >= MAX_CONSECUTIVE_OPTIMIZER_FAILURES:
                        raise GrowError(
                            f"optimizer failed {MAX_CONSECUTIVE_OPTIMIZER_FAILURES} times in a row: {reason}"
                        ) from exc
                    continue
                self._write_proposal(vdir, proposal)
                state.optimizer_cost_usd += proposal.cost_usd or 0.0
                candidate_content = {**current_bundle.content_files, **proposal.files}
                errors = lint_candidate(
                    current_bundle.content_files, candidate_content, secrets, constraints
                )
                if not errors:
                    candidate_files: dict[str, str | bytes] = dict(candidate_content)
                    if "harness.yaml" in current_bundle.files:
                        candidate_files["harness.yaml"] = current_bundle.files["harness.yaml"]
                    try:
                        bundle = HarnessBundle.from_files(bundle_dir, candidate_files)
                    except BundleError as exc:
                        errors = [str(exc)]
                if errors:
                    reason = "lint: " + "; ".join(errors)
                    self._record_invalid(
                        session_id, state, number, current, vdir, reason, window, spec, proposal
                    )
                    report("version", f"v{number} invalid: {reason}", number)
                    save()
                    if state.consecutive_optimizer_failures >= MAX_CONSECUTIVE_OPTIMIZER_FAILURES:
                        raise GrowError(
                            f"optimizer produced {MAX_CONSECUTIVE_OPTIMIZER_FAILURES} invalid "
                            f"candidates in a row: {reason}"
                        )
                    continue
                state.consecutive_optimizer_failures = 0
                bundle.write_to(bundle_dir)
                vid = self.repo.create_harness_version(
                    session_id=session_id,
                    number=number,
                    parent_id=current.id,
                    harness_hash=bundle.hash,
                    bundle_path=self._relative(bundle_dir),
                    status="candidate",
                    optimizer_kind=spec.optimizer.kind,
                    optimizer_model=spec.optimizer.model,
                )
                self.repo.update_harness_version(
                    vid,
                    rationale=proposal.rationale,
                    optimizer_usage_json=proposal.usage.model_dump(mode="json"),
                    optimizer_cost_usd=proposal.cost_usd,
                    window_task_keys_json=list(window),
                )
                state.pending_version_id = vid
                save()
                runs, window_exp = await self._run_role(
                    session_id, spec, suite, state, vid, [by_id[t] for t in window], "window"
                )
                fixed = [t for t in window if self._all_passed(runs, t)]
                self.repo.update_harness_version(
                    vid, window_experiment_id=window_exp, window_fixed_json=fixed
                )
                if len(fixed) < spec.window.min_fixed:
                    reason = f"window: fixed {len(fixed)}/{len(window)} < {spec.window.min_fixed}"
                    self._reject(state, vid, reason, window, spec)
                    report("version", f"v{number} rejected: {reason}", number)
                    save()
                    continue
                stats = await self._evaluate(
                    session_id, spec, suite, state, vid, gate_tasks, "gate"
                )
                self.repo.update_harness_version(vid, **stats.fields())
                if stats.n_valid == 0:
                    reason = "gate: no valid runs"
                elif (
                    state.current_gate_pass_rate is not None
                    and stats.pass_rate is not None
                    and stats.pass_rate < state.current_gate_pass_rate
                ):
                    reason = f"gate: {stats.pass_rate:.2f} < {state.current_gate_pass_rate:.2f}"
                else:
                    reason = None
                if reason:
                    self._reject(state, vid, reason, window, spec)
                    report("version", f"v{number} rejected: {reason}", number)
                    save()
                    continue
                self.repo.update_harness_version(vid, status="accepted", finished_at=utcnow())
                state.current_version_id = vid
                state.current_gate_pass_rate = stats.pass_rate
                state.versions_accepted += 1
                state.pending_version_id = None
                for t in window:
                    if t in fixed:
                        state.pool.remove(t)
                    else:
                        self._bump(state, t, spec)
                report(
                    "version",
                    f"v{number} accepted: fixed {len(fixed)}/{len(window)}, gate "
                    f"{stats.pass_rate if stats.pass_rate is not None else '—'}",
                    number,
                )
                save()
            if state.phase == "iterating":
                state.phase = "final" if split.final else "done"
                save()
            if state.phase == "final":
                report("phase", "final holdout: initial and current harness")
                final_tasks = [by_id[t] for t in split.final]
                versions = [state.initial_version_id]
                if state.current_version_id != state.initial_version_id:
                    versions.append(state.current_version_id)
                for vid in versions:
                    if self._over_budget(state, budget, len(final_tasks) * spec.repetitions):
                        break
                    await self._run_role(session_id, spec, suite, state, vid, final_tasks, "final")
                state.phase = "done"
                save()
        except asyncio.CancelledError:
            status = "interrupted"
            save()
            self.repo.update_grow_session(session_id, status=status)
            raise
        except Exception as exc:
            status = "failed"
            state.notes.append(f"{type(exc).__name__}: {exc}")
            state.notes.append(traceback.format_exc()[-2000:])
            save()
            self.repo.update_grow_session(
                session_id, status=status, finished_at=utcnow(), notes="\n".join(state.notes)
            )
            raise
        self.repo.update_grow_session(
            session_id,
            status=status,
            finished_at=utcnow(),
            notes="\n".join(state.notes) or None,
            current_version_id=state.current_version_id,
        )
        return GrowOutcome(
            session_id=session_id,
            status=status,
            iterations=state.iteration,
            initial_version_id=state.initial_version_id,
            current_version_id=state.current_version_id,
            versions_accepted=state.versions_accepted,
            versions_rejected=state.versions_rejected,
            versions_invalid=state.versions_invalid,
            notes=list(state.notes),
        )

    # ------------------------------------------------------------------
    async def _run_role(
        self,
        session_id: str,
        spec: GrowSpec,
        suite: SuiteSpec,
        state: GrowState,
        version_id: str,
        tasks: list[TaskSpec],
        role: str,
    ) -> tuple[list[RunOutcome], str]:
        version = self.repo.get_harness_version(version_id)
        if version is None:
            raise GrowError(f"harness version not found: {version_id}")
        bundle_dir = self.settings.home / version.bundle_path
        base = dict(spec.base_variant)
        overrides = dict(base.get("gate_overrides") or {}) if role in GATE_ROLES else {}
        runner = str(overrides.get("runner", base["runner"]))
        model = overrides.get("model", base.get("model"))
        options = {k: v for k, v in base.items() if k not in VARIANT_RESERVED}
        options.update({k: v for k, v in overrides.items() if k not in ("runner", "model")})
        variant = VariantSpec(
            id=f"v{version.number}", runner=runner, model=model, harness=str(bundle_dir), **options
        )
        variant.harness_dir = bundle_dir
        variant.harness_hash = version.harness_hash
        experiment = ExperimentSpec(
            name=f"{spec.name}/{role}/v{version.number}",
            suite=str(suite.source_path) if suite.source_path else spec.suite,
            repetitions=spec.repetitions,
            parallelism=spec.parallelism,
            keep_worktrees=spec.keep_worktrees,
            plugins=spec.plugins,
            source_path=suite.source_path,
        )
        outcome = await self.experiments.run_experiment(experiment, suite, tasks, [variant])
        self.repo.tag_experiment_grow(outcome.experiment_id, session_id, role)
        state.runs_started += len(outcome.runs)
        for r in outcome.runs:
            cost = r.metrics.reported_cost_usd
            if cost is None:
                cost = r.metrics.estimated_cost_usd
            state.deployed_cost_usd += float(cost or 0.0)
        return outcome.runs, outcome.experiment_id

    async def _evaluate(
        self,
        session_id: str,
        spec: GrowSpec,
        suite: SuiteSpec,
        state: GrowState,
        version_id: str,
        tasks: list[TaskSpec],
        role: str,
    ) -> GateStats:
        runs, exp_id = await self._run_role(session_id, spec, suite, state, version_id, tasks, role)
        return GateStats.from_runs(runs, exp_id)

    # ------------------------------------------------------------------
    @staticmethod
    def _all_passed(runs: list[RunOutcome], task_id: str) -> bool:
        mine = [r for r in runs if r.task_key == task_id]
        return bool(mine) and all(r.metrics.verified_pass is True for r in mine)

    @staticmethod
    def _window(state: GrowState, spec: GrowSpec, split: ResolvedSplit) -> list[str]:
        order = {t: i for i, t in enumerate(split.train)}
        ranked = sorted(state.pool, key=lambda t: (state.attempts.get(t, 0), order.get(t, 0)))
        return ranked[: spec.window.size]

    @staticmethod
    def _bump(state: GrowState, task_id: str, spec: GrowSpec) -> None:
        state.attempts[task_id] = state.attempts.get(task_id, 0) + 1
        if state.attempts[task_id] >= spec.window.max_attempts and task_id in state.pool:
            state.pool.remove(task_id)
            state.retired.append(task_id)

    def _reject(
        self, state: GrowState, vid: str, reason: str, window: list[str], spec: GrowSpec
    ) -> None:
        self.repo.update_harness_version(
            vid, status="rejected", reason=reason, finished_at=utcnow()
        )
        state.versions_rejected += 1
        state.pending_version_id = None
        state.previous_rejections = (state.previous_rejections + [reason])[-5:]
        for t in window:
            self._bump(state, t, spec)

    def _record_invalid(
        self,
        session_id: str,
        state: GrowState,
        number: int,
        current: Any,
        vdir: Path,
        reason: str,
        window: list[str],
        spec: GrowSpec,
        proposal: Proposal | None,
    ) -> None:
        if proposal is not None:
            self._write_proposal(vdir, proposal)
            state.optimizer_cost_usd += proposal.cost_usd or 0.0
        vid = self.repo.create_harness_version(
            session_id=session_id,
            number=number,
            parent_id=current.id,
            harness_hash=current.harness_hash,
            bundle_path=current.bundle_path,
            status="invalid",
            optimizer_kind=spec.optimizer.kind,
            optimizer_model=spec.optimizer.model,
        )
        self.repo.update_harness_version(
            vid,
            reason=reason,
            window_task_keys_json=list(window),
            rationale=proposal.rationale if proposal else None,
            optimizer_cost_usd=proposal.cost_usd if proposal else None,
            finished_at=utcnow(),
        )
        state.versions_invalid += 1
        state.consecutive_optimizer_failures += 1
        state.previous_rejections = (state.previous_rejections + [reason])[-5:]
        for t in window:
            self._bump(state, t, spec)

    @staticmethod
    def _write_proposal(vdir: Path, proposal: Proposal) -> None:
        payload = {
            "rationale": proposal.rationale,
            "files": proposal.files,
            "usage": proposal.usage.model_dump(mode="json"),
            "cost_usd": proposal.cost_usd,
            "raw": proposal.raw,
        }
        (vdir / "proposal.json").write_text(json.dumps(payload, indent=2, default=str))

    def _over_budget(self, state: GrowState, budget: GrowBudget | None, planned: int) -> bool:
        if budget is None:
            return False
        if budget.max_runs is not None and state.runs_started + planned > budget.max_runs:
            state.notes.append(
                f"stopped: budget max_runs {budget.max_runs} would be exceeded "
                f"({state.runs_started} started, {planned} planned)"
            )
            return True
        if budget.max_cost_usd is not None and state.deployed_cost_usd >= budget.max_cost_usd:
            state.notes.append(
                f"stopped: budget max_cost_usd {budget.max_cost_usd} reached "
                f"(spent {state.deployed_cost_usd:.4f})"
            )
            return True
        return False

    @staticmethod
    def _optimizer_over_budget(state: GrowState, budget: GrowBudget | None) -> bool:
        if budget is None or budget.max_optimizer_cost_usd is None:
            return False
        if state.optimizer_cost_usd >= budget.max_optimizer_cost_usd:
            state.notes.append(
                f"stopped: budget max_optimizer_cost_usd {budget.max_optimizer_cost_usd} reached "
                f"(spent {state.optimizer_cost_usd:.4f})"
            )
            return True
        return False

    def _relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.settings.home.resolve()))


class GateStats(BaseModel):
    experiment_id: str
    n_valid: int = 0
    n_passed: int = 0
    pass_rate: float | None = None
    llm_calls_median: float | None = None
    cost_median: float | None = None

    @classmethod
    def from_runs(cls, runs: list[RunOutcome], experiment_id: str) -> GateStats:
        valid = [r for r in runs if r.metrics.verified_pass is not None]
        passed = sum(1 for r in valid if r.metrics.verified_pass)
        calls = [float(r.metrics.llm_calls) for r in valid if r.metrics.llm_calls is not None]
        costs: list[float] = []
        for r in valid:
            cost = r.metrics.reported_cost_usd
            if cost is None:
                cost = r.metrics.estimated_cost_usd
            if cost is not None:
                costs.append(float(cost))
        return cls(
            experiment_id=experiment_id,
            n_valid=len(valid),
            n_passed=passed,
            pass_rate=(passed / len(valid)) if valid else None,
            llm_calls_median=statistics.median(calls) if calls else None,
            cost_median=statistics.median(costs) if costs else None,
        )

    def fields(self) -> dict[str, Any]:
        return {
            "gate_experiment_id": self.experiment_id,
            "gate_pass_rate": self.pass_rate,
            "gate_n_valid": self.n_valid,
            "gate_n_passed": self.n_passed,
            "gate_llm_calls_median": self.llm_calls_median,
            "gate_cost_median": self.cost_median,
        }

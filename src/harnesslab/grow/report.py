"""Reports for grow sessions: the version lineage (the growth curve) and the final holdout."""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from harnesslab.storage.repository import Repository


class VersionReport(BaseModel):
    id: str
    number: int
    status: str
    reason: str | None = None
    harness_hash: str
    parent_number: int | None = None
    window_task_keys: list[str] = Field(default_factory=list)
    window_fixed: list[str] = Field(default_factory=list)
    gate_pass_rate: float | None = None
    gate_n_valid: int | None = None
    gate_n_passed: int | None = None
    llm_calls_median: float | None = None
    cost_median: float | None = None
    optimizer_kind: str | None = None
    optimizer_model: str | None = None
    optimizer_cost_usd: float | None = None
    rationale: str | None = None
    window_experiment_id: str | None = None
    gate_experiment_id: str | None = None
    created_at: datetime | None = None


class FinalSide(BaseModel):
    version_number: int
    experiment_id: str
    n_valid: int = 0
    pass_rate: float | None = None
    llm_calls_median: float | None = None
    cost_median: float | None = None


class FinalComparison(BaseModel):
    task_keys: list[str]
    initial: FinalSide
    current: FinalSide


class GrowReport(BaseModel):
    session_id: str
    name: str
    suite_name: str
    status: str
    phase: str | None
    iterations: int
    minimize: str
    initial: VersionReport | None
    current: VersionReport | None
    versions: list[VersionReport]
    final: FinalComparison | None = None
    notes: list[str] = Field(default_factory=list)
    deployed_cost_usd: float = 0.0
    optimizer_cost_usd: float = 0.0
    runs_started: int = 0


def _version_report(row: Any, numbers: dict[str, int]) -> VersionReport:
    return VersionReport(
        id=row.id,
        number=row.number,
        status=row.status,
        reason=row.reason,
        harness_hash=row.harness_hash,
        parent_number=numbers.get(row.parent_id) if row.parent_id else None,
        window_task_keys=list(row.window_task_keys_json or []),
        window_fixed=list(row.window_fixed_json or []),
        gate_pass_rate=row.gate_pass_rate,
        gate_n_valid=row.gate_n_valid,
        gate_n_passed=row.gate_n_passed,
        llm_calls_median=row.gate_llm_calls_median,
        cost_median=row.gate_cost_median,
        optimizer_kind=row.optimizer_kind,
        optimizer_model=row.optimizer_model,
        optimizer_cost_usd=row.optimizer_cost_usd,
        rationale=row.rationale,
        window_experiment_id=row.window_experiment_id,
        gate_experiment_id=row.gate_experiment_id,
        created_at=row.created_at,
    )


def _final_side(repo: Repository, exp_row: Any, number: int) -> FinalSide:
    full = repo.get_experiment(exp_row.id)
    runs = list(full.runs) if full else []
    valid = [r for r in runs if r.verified_pass is not None]
    calls = [float(r.llm_calls) for r in valid if r.llm_calls is not None]
    costs = [
        float(r.reported_cost_usd if r.reported_cost_usd is not None else r.estimated_cost_usd)
        for r in valid
        if (r.reported_cost_usd is not None or r.estimated_cost_usd is not None)
    ]
    passed = sum(1 for r in valid if r.verified_pass)
    return FinalSide(
        version_number=number,
        experiment_id=exp_row.id,
        n_valid=len(valid),
        pass_rate=(passed / len(valid)) if valid else None,
        llm_calls_median=statistics.median(calls) if calls else None,
        cost_median=statistics.median(costs) if costs else None,
    )


def report_for_session(repo: Repository, session: Any) -> GrowReport:
    versions = list(session.versions)
    numbers = {v.id: v.number for v in versions}
    reports = [_version_report(v, numbers) for v in versions]
    by_id = {r.id: r for r in reports}
    state = session.state_json or {}
    spec = session.spec_json or {}
    minimize = str((spec.get("report") or {}).get("minimize", "llm_calls"))

    final: FinalComparison | None = None
    finals = [
        e
        for e in repo.list_experiments()
        if e["grow_session_id"] == session.id and e["grow_role"] == "final"
    ]
    if finals:
        by_version: dict[int, Any] = {}
        for entry in finals:
            row = repo.get_experiment(entry["id"])
            if row is None or not row.variants:
                continue
            key = row.variants[0].variant_key  # "v<N>"
            try:
                by_version[int(key[1:])] = row
            except ValueError:
                continue
        initial_row = by_id.get(session.initial_version_id)
        current_row = by_id.get(session.current_version_id)
        if initial_row and current_row and initial_row.number in by_version:
            initial_exp = by_version[initial_row.number]
            current_exp = by_version.get(current_row.number, initial_exp)
            task_keys = [t.task_key for t in repo.get_experiment(initial_exp.id).tasks]
            final = FinalComparison(
                task_keys=task_keys,
                initial=_final_side(repo, initial_exp, initial_row.number),
                current=_final_side(repo, current_exp, current_row.number),
            )

    notes = [n for n in (session.notes or "").split("\n") if n]
    return GrowReport(
        session_id=session.id,
        name=session.name,
        suite_name=session.suite_name,
        status=session.status,
        phase=session.phase,
        iterations=session.iterations or 0,
        minimize=minimize,
        initial=by_id.get(session.initial_version_id),
        current=by_id.get(session.current_version_id),
        versions=reports,
        final=final,
        notes=notes,
        deployed_cost_usd=float(state.get("deployed_cost_usd") or 0.0),
        optimizer_cost_usd=float(state.get("optimizer_cost_usd") or 0.0),
        runs_started=int(state.get("runs_started") or 0),
    )


def lineage_json(report: GrowReport) -> dict[str, Any]:
    return report.model_dump(mode="json")

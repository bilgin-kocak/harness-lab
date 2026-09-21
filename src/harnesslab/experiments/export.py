"""Experiment export as JSON (reproducibility record + traces + verdicts)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from harnesslab.experiments.aggregate import aggregate_variants, samples_from_rows
from harnesslab.storage.repository import Repository

EXPORT_SCHEMA_VERSION = "1"


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def export_experiment(
    repo: Repository,
    exp_id: str,
    *,
    include_events: bool = True,
    include_artifacts: bool = True,
    artifact_cap: int | None = None,
) -> dict[str, Any] | None:
    exp = repo.get_experiment(exp_id)
    if exp is None:
        return None
    tasks_by_id = {t.id: t for t in exp.tasks}
    variants_by_id = {v.id: v for v in exp.variants}
    samples = samples_from_rows(exp.runs, tasks_by_id, variants_by_id)
    aggregates = aggregate_variants(samples, [v.variant_key for v in exp.variants])

    runs: list[dict[str, Any]] = []
    for run in exp.runs:
        full = repo.get_run(run.id)
        if full is None:
            continue
        record: dict[str, Any] = {
            "id": full.id,
            "task": tasks_by_id[full.task_id].task_key if full.task_id in tasks_by_id else None,
            "variant": variants_by_id[full.variant_id].variant_key if full.variant_id in variants_by_id else None,
            "repetition": full.repetition,
            "status": full.status,
            "outcome": full.outcome,
            "started_at": _dt(full.started_at),
            "finished_at": _dt(full.finished_at),
            "reproducibility": {
                "base_commit": full.base_commit,
                "task_hash": full.task_hash,
                "prompt_hash": full.prompt_hash,
                "harnesslab_commit": full.harnesslab_commit,
                "harnesslab_version": full.harnesslab_version,
                "runner": full.runner,
                "runner_config": full.runner_config_json,
                "config_hash": full.config_hash,
                "model_requested": full.model_requested,
                "model_resolved": full.model_resolved,
                "cli_version": full.cli_version,
                "environment": full.environment_json,
                "environment_hash": full.environment_hash,
                "parser_version": full.parser_version,
                "metrics_version": full.metrics_version,
                "provider_session_id": full.provider_session_id,
            },
            "metrics": full.metrics_json,
            "error_message": full.error_message,
            "final_message": full.final_message,
            "worktree_path": full.worktree_path if full.worktree_kept else None,
            "verifier": None,
            "artifacts": [],
        }
        if full.verifier_result is not None:
            vr = full.verifier_result
            record["verifier"] = {
                "passed": vr.passed,
                "outcome": vr.outcome,
                "command": vr.command,
                "exit_code": vr.exit_code,
                "timed_out": vr.timed_out,
                "duration_ms": vr.duration_ms,
                "stdout": vr.stdout,
                "stderr": vr.stderr,
                "score": vr.score,
                "max_score": vr.max_score,
                "normalized_score": vr.normalized_score,
                "score_metrics": vr.score_metrics_json,
                "score_command": vr.score_command,
                "score_exit_code": vr.score_exit_code,
                "score_error": vr.score_error,
                "protected_violations": vr.protected_violations_json,
                "injected_files": vr.injected_files_json,
                "skipped_reason": vr.skipped_reason,
                "verifier_version": vr.verifier_version,
            }
        for art in full.artifacts:
            entry: dict[str, Any] = {
                "kind": art.kind,
                "path": art.path,
                "media_type": art.media_type,
                "size_bytes": art.size_bytes,
                "sha256": art.sha256,
            }
            if include_artifacts and art.media_type.startswith("text/"):
                entry["content"] = repo.read_artifact(art, cap=artifact_cap)
            record["artifacts"].append(entry)
        if include_events:
            record["events"] = [
                {
                    "event_id": e.id,
                    "sequence": e.sequence,
                    "timestamp": _dt(e.timestamp),
                    "kind": e.kind,
                    "source": e.source,
                    "name": e.name,
                    "duration_ms": e.duration_ms,
                    "call_id": e.call_id,
                    "parent_call_id": e.parent_call_id,
                    "payload": e.payload_json,
                    "raw_metadata": e.raw_metadata_json,
                }
                for e in full.events
            ]
        runs.append(record)

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "experiment": {
            "id": exp.id,
            "name": exp.name,
            "description": exp.description,
            "status": exp.status,
            "suite_name": exp.suite_name,
            "suite_path": exp.suite_path,
            "spec": exp.spec_json,
            "repetitions": exp.repetitions,
            "parallelism": exp.parallelism,
            "created_at": _dt(exp.created_at),
            "finished_at": _dt(exp.finished_at),
            "harnesslab_version": exp.harnesslab_version,
            "harnesslab_commit": exp.harnesslab_commit,
            "environment": exp.environment_json,
            "environment_hash": exp.environment_hash,
        },
        "variants": [
            {
                "id": v.variant_key,
                "runner": v.runner,
                "model": v.model_requested,
                "description": v.description,
                "harness_version": v.harness_version,
                "model_provider": v.model_provider,
                "skill_version": v.skill_version,
                "context_policy": v.context_policy_json,
                "tool_policy": v.tool_policy_json,
                "config": v.config_json,
                "config_hash": v.config_hash,
            }
            for v in exp.variants
        ],
        "tasks": [
            {
                "id": t.task_key,
                "name": t.name,
                "version": t.version,
                "task_hash": t.task_hash,
                "spec_hash": t.spec_hash,
                "prompt_hash": t.prompt_hash,
                "base_commit": t.base_commit,
                "repo_path": t.repo_path,
                "tags": t.tags_json,
                "spec": t.spec_json,
            }
            for t in exp.tasks
        ],
        "aggregates": {k: v.model_dump(mode="json") for k, v in aggregates.items()},
        "runs": runs,
    }

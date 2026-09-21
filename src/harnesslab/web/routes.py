"""Dashboard routes (server-rendered HTML + a small JSON API)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from harnesslab.experiments.aggregate import (
    aggregate_variants,
    build_matrix,
    compare_variants,
    samples_from_rows,
)
from harnesslab.experiments.export import export_experiment
from harnesslab.storage.repository import Repository
from harnesslab.web.timeline import build_timeline

router = APIRouter()

DIFF_VIEW_CAP = 400_000


def _repo(request: Request) -> Repository:
    return request.app.state.repo


def _render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(request, template, context)


def _experiment_context(repo: Repository, exp_ref: str) -> dict[str, Any]:
    exp = repo.find_experiment(exp_ref)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    tasks_by_id = {t.id: t for t in exp.tasks}
    variants_by_id = {v.id: v for v in exp.variants}
    samples = samples_from_rows(exp.runs, tasks_by_id, variants_by_id)
    variant_keys = [v.variant_key for v in exp.variants]
    task_keys = [t.task_key for t in exp.tasks]
    return {
        "exp": exp,
        "tasks": exp.tasks,
        "variants": exp.variants,
        "task_keys": task_keys,
        "variant_keys": variant_keys,
        "samples": samples,
        "matrix": build_matrix(samples, task_keys, variant_keys),
        "aggregates": aggregate_variants(samples, variant_keys),
        "runs_by_id": {r.id: r for r in exp.runs},
        "tasks_by_id": tasks_by_id,
        "variants_by_id": variants_by_id,
    }


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def experiments_page(request: Request) -> HTMLResponse:
    rows = _repo(request).list_experiments()
    return _render(request, "experiments.html", {"experiments": rows})


@router.get("/experiments/{exp_id}", response_class=HTMLResponse)
def experiment_page(request: Request, exp_id: str) -> HTMLResponse:
    ctx = _experiment_context(_repo(request), exp_id)
    return _render(request, "experiment_detail.html", ctx)


@router.get("/experiments/{exp_id}/compare", response_class=HTMLResponse)
def compare_page(
    request: Request, exp_id: str, a: str | None = None, b: str | None = None
) -> HTMLResponse:
    ctx = _experiment_context(_repo(request), exp_id)
    keys = ctx["variant_keys"]
    if not keys:
        raise HTTPException(status_code=404, detail="experiment has no variants")
    a = a if a in keys else keys[0]
    b = b if b in keys else (keys[1] if len(keys) > 1 else keys[0])
    comparison = compare_variants(ctx["samples"], a, b, ctx["task_keys"])
    ctx.update({"a": a, "b": b, "comparison": comparison})
    template = "partials/compare_body.html" if request.headers.get("HX-Request") else "compare.html"
    return _render(request, template, ctx)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str) -> HTMLResponse:
    repo = _repo(request)
    run = repo.find_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    timeline = build_timeline(run.events)
    t0 = run.events[0].timestamp if run.events else None
    for item in timeline:
        item.payload = dict(item.payload)
        item.payload["_offset_s"] = round((item.timestamp - t0).total_seconds(), 2) if t0 else None
    artifacts = {a.kind: a for a in run.artifacts}
    diff_text = (
        repo.read_artifact(artifacts["agent_diff"], cap=DIFF_VIEW_CAP)
        if "agent_diff" in artifacts
        else ""
    )
    diff_stat = (
        repo.read_artifact(artifacts["diff_stat"], cap=20_000) if "diff_stat" in artifacts else ""
    )
    git_status = (
        repo.read_artifact(artifacts["git_status"], cap=20_000) if "git_status" in artifacts else ""
    )
    metrics = run.metrics_json or {}
    return _render(
        request,
        "run_detail.html",
        {
            "run": run,
            "task": run.task,
            "variant": run.variant,
            "exp": run.experiment,
            "metrics": metrics,
            "timeline": timeline,
            "artifacts": artifacts,
            "diff_text": diff_text,
            "diff_stat": diff_stat,
            "git_status": git_status,
            "verifier": run.verifier_result,
            "event_count": len(run.events),
        },
    )


@router.get("/runs/{run_id}/artifacts/{kind}", response_class=PlainTextResponse)
def run_artifact(request: Request, run_id: str, kind: str) -> PlainTextResponse:
    repo = _repo(request)
    run = repo.find_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    for artifact in run.artifacts:
        if artifact.kind == kind:
            return PlainTextResponse(
                repo.read_artifact(artifact), media_type=artifact.media_type or "text/plain"
            )
    raise HTTPException(status_code=404, detail="artifact not found")


@router.get("/api/experiments/{exp_id}/export.json")
def api_export(
    request: Request, exp_id: str, events: bool = True, artifacts: bool = True
) -> JSONResponse:
    repo = _repo(request)
    exp = repo.find_experiment(exp_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return JSONResponse(
        export_experiment(repo, exp.id, include_events=events, include_artifacts=artifacts)
    )


@router.get("/api/experiments/{exp_id}/runs.json")
def api_runs(request: Request, exp_id: str) -> JSONResponse:
    ctx = _experiment_context(_repo(request), exp_id)
    return JSONResponse(
        {
            "runs": [s.model_dump() for s in ctx["samples"]],
            "aggregates": {k: v.model_dump() for k, v in ctx["aggregates"].items()},
        }
    )


@router.get("/api/runs/{run_id}/events.json")
def api_events(request: Request, run_id: str) -> JSONResponse:
    repo = _repo(request)
    run = repo.find_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return JSONResponse(
        {
            "run_id": run.id,
            "events": [
                {
                    "event_id": e.id,
                    "sequence": e.sequence,
                    "timestamp": e.timestamp.isoformat(),
                    "kind": e.kind,
                    "source": e.source,
                    "name": e.name,
                    "duration_ms": e.duration_ms,
                    "call_id": e.call_id,
                    "parent_call_id": e.parent_call_id,
                    "payload": e.payload_json,
                }
                for e in run.events
            ],
        }
    )

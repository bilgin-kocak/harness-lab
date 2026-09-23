"""Build what the optimizer is allowed to see from stored runs.

Hidden test content never enters the view: every text field is passed through
:meth:`harnesslab.harness.lint.SuiteSecrets.scrub`, injected files are never read, and
the trace digest keeps only compact previews.
"""

from __future__ import annotations

from typing import Any

from harnesslab.core.models import TaskSpec
from harnesslab.grow.optimizers.base import (
    EditConstraintsSpec,
    FailureCase,
    FailureMetrics,
    OptimizerContext,
)
from harnesslab.harness.bundle import HarnessBundle
from harnesslab.harness.lint import EditConstraints, SuiteSecrets
from harnesslab.storage.repository import Repository

DIGEST_LIMIT = 60
DIFF_CAP = 20_000
VERIFIER_CAP = 8_000
MESSAGE_CAP = 2_000
PREVIEW_CHARS = 200
SKIPPED_KINDS = {"run_started", "run_finished", "usage", "system"}
ALLOWED_PATHS = [
    "system_prompt.md",
    "hooks.json",
    "fake.yaml",
    "skills/<name>/SKILL.md",
    "agents/<name>.md",
]


def capped_scrub(secrets: SuiteSecrets, text: str, cap: int, *, tail: bool = False) -> str:
    """Scrub first, then truncate, so a hidden name cut at the boundary cannot slip through."""
    scrubbed = secrets.scrub(text)
    if len(scrubbed) <= cap:
        return scrubbed
    return scrubbed[-cap:] if tail else scrubbed[:cap]


def trace_digest(events: list[Any], secrets: SuiteSecrets, limit: int = DIGEST_LIMIT) -> list[str]:
    """One compact, scrubbed line per tool/command/message/error event."""
    lines: list[str] = []
    for e in events:
        payload = getattr(e, "payload_json", None)
        if payload is None:
            payload = getattr(e, "payload", None) or {}
        kind = str(e.kind)
        if kind in SKIPPED_KINDS:
            continue
        head = payload.get("command") or e.name or ""
        extra: list[str] = []
        if payload.get("exit_code") is not None:
            extra.append(f"exit={payload['exit_code']}")
        if e.duration_ms is not None:
            extra.append(f"{e.duration_ms}ms")
        out = payload.get("output") or payload.get("stdout") or payload.get("text") or ""
        preview = capped_scrub(secrets, " ".join(str(out).split()), PREVIEW_CHARS)
        line = f"#{e.sequence} {kind} {capped_scrub(secrets, str(head), 120)} {' '.join(extra)}"
        line = line.rstrip()
        if preview:
            line += f" | {preview}"
        lines.append(line)
        if len(lines) >= limit:
            lines.append(f"... ({len(events)} events total)")
            break
    return lines


def build_failure_case(
    repo: Repository, run: Any, task: TaskSpec, secrets: SuiteSecrets, attempts: int
) -> FailureCase:
    artifacts = {a.kind: a for a in run.artifacts}
    diff = ""
    if "agent_diff" in artifacts:
        diff = repo.read_artifact(artifacts["agent_diff"], cap=DIFF_CAP * 2)
    verifier = run.verifier_result
    metrics = run.metrics_json or {}
    return FailureCase(
        task_id=task.id,
        task_name=task.name,
        prompt=task.prompt,
        attempts=attempts,
        run_id=run.id,
        status=run.status,
        outcome=run.outcome,
        verified_score=run.verified_score,
        final_message=capped_scrub(secrets, run.final_message or "", MESSAGE_CAP),
        trace_digest=trace_digest(run.events, secrets),
        diff=capped_scrub(secrets, diff, DIFF_CAP),
        verifier_stdout=capped_scrub(
            secrets, verifier.stdout if verifier else "", VERIFIER_CAP, tail=True
        ),
        verifier_stderr=capped_scrub(
            secrets, verifier.stderr if verifier else "", VERIFIER_CAP, tail=True
        ),
        metrics=FailureMetrics(
            llm_calls=metrics.get("llm_calls"),
            total_tokens=metrics.get("total_tokens"),
            wall_time_seconds=metrics.get("wall_time_seconds"),
            reported_cost_usd=metrics.get("reported_cost_usd"),
        ),
    )


def build_context(
    *,
    session_name: str,
    iteration: int,
    runner: str,
    model: str | None,
    bundle: HarnessBundle,
    cases: list[FailureCase],
    constraints: EditConstraints,
    previous_rejections: list[str],
    suite_description: str | None,
) -> OptimizerContext:
    return OptimizerContext(
        session_name=session_name,
        iteration=iteration,
        runner=runner,
        model=model,
        bundle=bundle.content_files,
        constraints=EditConstraintsSpec(
            allowed_paths=list(ALLOWED_PATHS),
            max_files=constraints.max_files,
            max_file_bytes=constraints.max_file_bytes,
            max_bundle_bytes=constraints.max_bundle_bytes,
        ),
        failures=list(cases),
        previous_rejections=list(previous_rejections)[-5:],
        suite_description=suite_description,
    )

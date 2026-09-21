"""Optional partial-score verifier.

``verification.score_command`` runs after the pass/fail command and must
produce JSON of the shape::

    {"score": 0.8, "max_score": 1.0, "metrics": {"tests_passed": 8, "tests_total": 10}}

either by writing it to the file named in ``$HARNESSLAB_SCORE_FILE`` or by
printing it as the last JSON object on stdout.  The pass/fail command remains
the authority on ``verified_pass``; the score only refines ``verified_score``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harnesslab.core.models import TaskSpec, VerifierResult
from harnesslab.execution.sandbox import ExecutionSandbox, SandboxContext
from harnesslab.trace.redaction import Redactor, default_redactor


def parse_score_output(text: str) -> dict[str, Any] | None:
    """Return the last JSON object found in ``text`` (whole text first, then line by line)."""
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def normalize_score(obj: dict[str, Any]) -> tuple[float, float, float, dict[str, Any]]:
    score = float(obj["score"])
    max_score = float(obj.get("max_score", 1.0))
    if max_score <= 0:
        raise ValueError("max_score must be positive")
    normalized = max(0.0, min(1.0, score / max_score))
    metrics = obj.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {"value": metrics}
    return score, max_score, normalized, metrics


class ScoreVerifier:
    def __init__(self, redactor: Redactor | None = None) -> None:
        self.redactor = redactor or default_redactor()

    async def apply(
        self,
        task: TaskSpec,
        sandbox: ExecutionSandbox,
        ctx: SandboxContext,
        result: VerifierResult,
    ) -> VerifierResult:
        command = task.verification.score_command
        assert command
        score_file = ctx.workdir.parent / f"{ctx.run_id}.score.json"
        proc = await sandbox.run_command(
            ctx,
            command,
            timeout=task.verification.timeout_seconds,
            include_auth=False,
            env_overrides={"HARNESSLAB_SCORE_FILE": str(score_file)},
        )
        result.score_exit_code = proc.exit_code
        raw: str | None = None
        if score_file.exists():
            try:
                raw = score_file.read_text(encoding="utf-8", errors="replace")
            finally:
                Path(score_file).unlink(missing_ok=True)
        if not raw:
            raw = proc.stdout
        try:
            if proc.error:
                raise RuntimeError(proc.error)
            if proc.timed_out:
                raise RuntimeError("score command timed out")
            obj = parse_score_output(raw or "")
            if obj is None:
                raise ValueError("no JSON object found in score output")
            score, max_score, normalized, metrics = normalize_score(obj)
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            result.score_error = self.redactor.redact_text(f"{exc}")
            if proc.stderr_tail:
                result.stderr = (
                    result.stderr
                    + "\n[score] "
                    + self.redactor.redact_text(proc.stderr_tail[-2000:])
                ).strip()
            return result
        result.score = score
        result.max_score = max_score
        result.normalized_score = normalized
        result.score_metrics = self.redactor.redact_value(metrics)
        return result

"""Exit-code verifier with hidden-file injection and protected-path checks."""

from __future__ import annotations

import fnmatch
import os
import shutil

from harnesslab.core.models import DiffSummary, Outcome, TaskSpec, VerifierResult
from harnesslab.execution.sandbox import ExecutionSandbox, SandboxContext
from harnesslab.trace.redaction import Redactor, default_redactor
from harnesslab.verification.base import Verifier
from harnesslab.verification.score import ScoreVerifier

OUTPUT_CAP = 200_000


def _matches_protected(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        pat = pattern.rstrip("/")
        if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat + "/"):
            return True
        if fnmatch.fnmatch(path, pat + "/*") or fnmatch.fnmatch(path, pat + "/**"):
            return True
    return False


def protected_violations(changes: DiffSummary, patterns: list[str]) -> list[str]:
    """Changed paths (created, modified, deleted or renamed) that hit a protected pattern."""
    if not patterns:
        return []
    violations: list[str] = []
    for f in changes.files:
        candidates = [f.path]
        if " => " in f.path:  # rename: "old => new" or "dir/{old => new}/file"
            candidates = [p.strip("{} ") for p in f.path.split(" => ")]
        if any(_matches_protected(c, patterns) for c in candidates):
            violations.append(f.path)
    return violations


def _cap(text: str, cap: int = OUTPUT_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n... [truncated {len(text) - cap} characters]"


class CommandVerifier(Verifier):
    """Runs ``verification.command``; exit code 0 means the task passed.

    Steps: protected paths -> inject hidden files -> run command -> optional
    partial-score command (see :class:`ScoreVerifier`).
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        self.redactor = redactor or default_redactor()
        self.score_verifier = ScoreVerifier(redactor=self.redactor)

    def inject_files(self, task: TaskSpec, ctx: SandboxContext) -> tuple[list[str], list[str]]:
        injected: list[str] = []
        overwritten: list[str] = []
        for item in task.verification.inject:
            src = task.resolve(item.source)
            dest = (ctx.workdir / item.dest).resolve()
            if not str(dest).startswith(str(ctx.workdir.resolve()) + os.sep):
                raise ValueError(f"inject dest escapes the worktree: {item.dest}")
            if not src.exists():
                raise FileNotFoundError(f"inject source missing: {src}")
            if dest.exists():
                overwritten.append(item.dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
            injected.append(item.dest)
        return injected, overwritten

    async def verify(
        self,
        task: TaskSpec,
        sandbox: ExecutionSandbox,
        ctx: SandboxContext,
        changes: DiffSummary,
    ) -> VerifierResult:
        spec = task.verification
        result = VerifierResult(command=spec.command, score_command=spec.score_command)

        violations = protected_violations(changes, spec.protected_paths)
        if violations:
            result.protected_violations = violations
            result.passed = False
            result.outcome = Outcome.FAIL
            result.skipped_reason = "protected paths were modified: " + ", ".join(violations)
            result.stderr = result.skipped_reason
            return result

        try:
            result.injected_files, result.overwritten_files = self.inject_files(task, ctx)
        except (FileNotFoundError, ValueError) as exc:
            result.passed = None
            result.outcome = Outcome.NOT_VERIFIED
            result.skipped_reason = f"verifier setup failed: {exc}"
            result.stderr = result.skipped_reason
            return result

        stderr_path = ctx.workdir.parent / f"{ctx.run_id}.verifier.stderr"
        proc = await sandbox.run_command(
            ctx,
            spec.command,
            timeout=spec.timeout_seconds,
            include_auth=False,
            stderr_path=stderr_path,
        )
        try:
            stderr_path.unlink(missing_ok=True)
        except OSError:
            pass
        result.exit_code = proc.exit_code
        result.timed_out = proc.timed_out
        result.duration_ms = proc.duration_ms
        result.stdout = self.redactor.redact_text(_cap(proc.stdout))
        result.stderr = self.redactor.redact_text(_cap(proc.stderr_tail))
        if proc.error:
            result.passed = None
            result.outcome = Outcome.NOT_VERIFIED
            result.skipped_reason = proc.error
            result.stderr = (result.stderr + "\n" + proc.error).strip()
            return result
        if proc.timed_out:
            result.passed = False
            result.outcome = Outcome.FAIL
            result.stderr = (
                result.stderr + f"\n[verifier timed out after {spec.timeout_seconds}s]"
            ).strip()
        else:
            result.passed = proc.exit_code == 0
            result.outcome = Outcome.PASS if result.passed else Outcome.FAIL

        if spec.score_command:
            await self.score_verifier.apply(task, sandbox, ctx, result)
        return result

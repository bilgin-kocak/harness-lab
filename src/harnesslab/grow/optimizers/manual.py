"""Human-in-the-loop optimizer.

Writes the optimizer view and a copy of the current bundle to ``<version>-proposal/``,
waits for you to edit ``candidate/`` (and optionally write ``RATIONALE.md``), then reads the
candidate back.  The grow service still runs window, gate and rollback on your edit.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from harnesslab.grow.optimizers.base import (
    Optimizer,
    OptimizerContext,
    OptimizerError,
    Proposal,
    register_optimizer,
)

README = """Harness Lab manual optimizer
============================

1. Read context.json: the current bundle, the failing tasks (scrubbed), the constraints.
2. Edit the files under candidate/ (add skills/<name>/SKILL.md, change system_prompt.md, ...).
   Do not delete files, do not mention task ids or hidden test names.
3. Optionally write RATIONALE.md explaining the change.
4. Press Enter in the terminal running `harnesslab grow`.
"""


@register_optimizer
class ManualOptimizer(Optimizer):
    name = "manual"
    description = "You edit the candidate bundle; Harness Lab evaluates it."

    def proposal_dir(self) -> Path:
        base = self.artifacts_dir or Path.cwd() / "harnesslab-proposal"
        return base.parent / f"{base.name}-proposal"

    async def propose(self, context: OptimizerContext) -> Proposal:
        if not sys.stdin.isatty():
            raise OptimizerError(
                "the manual optimizer needs an interactive terminal (stdin is not a TTY)"
            )
        pdir = self.proposal_dir()
        candidate = pdir / "candidate"
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(parents=True)
        for rel, content in context.bundle.items():
            target = candidate / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        (pdir / "context.json").write_text(context.model_dump_json(indent=2), encoding="utf-8")
        (pdir / "README.txt").write_text(README, encoding="utf-8")
        print(f"\nManual optimizer: edit {candidate} (see {pdir / 'README.txt'})")
        input("Press Enter when the candidate is ready... ")
        files: dict[str, str] = {}
        for path in sorted(candidate.rglob("*")):
            if path.is_file():
                files[path.relative_to(candidate).as_posix()] = path.read_text(encoding="utf-8")
        rationale_path = pdir / "RATIONALE.md"
        rationale = (
            rationale_path.read_text(encoding="utf-8").strip() if rationale_path.exists() else ""
        )
        return Proposal(files=files, rationale=rationale or "manual edit", raw={"manual": True})

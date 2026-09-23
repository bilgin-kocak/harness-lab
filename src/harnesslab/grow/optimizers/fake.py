"""Fake optimizer for tests and the demo.

It "learns" by adding the window's task ids to ``fake.yaml``, which the fake runner reads,
so the whole grow loop can be exercised in seconds without credentials.  Options::

    fix_none: true             propose the unchanged bundle (window rejection path)
    regress_gate_task: <id>    also add a gate task to fail_tasks (gate rollback path)
    invalid: true              propose a forbidden path (invalid proposal path)
    llm_calls: 2               simulated llm_calls written into fake.yaml
"""

from __future__ import annotations

import yaml

from harnesslab.grow.optimizers.base import (
    Optimizer,
    OptimizerContext,
    Proposal,
    register_optimizer,
)


@register_optimizer
class FakeOptimizer(Optimizer):
    name = "fake"
    description = "Simulated optimizer that marks failing tasks as solved in fake.yaml."

    async def propose(self, context: OptimizerContext) -> Proposal:
        files = dict(context.bundle)
        if self.options.get("invalid"):
            files["../escape.md"] = "escape"
            return Proposal(files=files, rationale="deliberately invalid proposal", raw={"fake": 1})
        if self.options.get("fix_none"):
            return Proposal(files=files, rationale="no change proposed", raw={"fake": 1})
        data = yaml.safe_load(files.get("fake.yaml") or "") or {}
        solve = list(data.get("solve_tasks") or [])
        for case in context.failures:
            if case.task_id not in solve:
                solve.append(case.task_id)
        data["solve_tasks"] = solve
        regress = self.options.get("regress_gate_task")
        if regress:
            solve = [t for t in solve if t != regress]
            data["solve_tasks"] = solve
            fail = list(data.get("fail_tasks") or [])
            if regress not in fail:
                fail.append(regress)
            data["fail_tasks"] = fail
        data["llm_calls"] = int(self.options.get("llm_calls", 2))
        files["fake.yaml"] = yaml.safe_dump(data, sort_keys=False)
        solved = ", ".join(c.task_id for c in context.failures)
        return Proposal(files=files, rationale=f"fake optimizer: solve {solved}", raw={"fake": 1})

# The Growing Harness paper

*Grow the Harness, Not the Context: From Strategy-Free Scaffolds to Reusable Specialist Agents*
(Li, Li, Zhao, Ye, Li, Xu, Gao; arXiv 2609.26760, September 2026) trains the harness instead of
the model. This page maps its method onto Harness Lab and records where the two differ.

## The paper in one paragraph

Start from a minimal executable scaffold with no strategy. Run tasks; collect failures into a
window of size K. Build a function-level execution trace per failure. An optimizer model edits only
the functions that appear in failing traces, at most L functions, keeping signatures, never
deleting, and never encoding task ids or expected answers. Re-run the candidate on the window: it
must fix at least Q failures, and its success rate on a fixed held-out gate must not drop.
Otherwise the whole repair rolls back. Across BrowseComp-Plus and WebArena-Verified with deployed
models from 4B to 120B parameters, the grown harness reduced LLM calls by 76 to 92 percent and
deployed-model cost by 74 to 99 percent versus a tool-calling agent, at equal or better success.
The ablations matter most: without the gate, success climbed then decayed; without trace locality,
success halved.

## Mapping

| Paper | Harness Lab |
| --- | --- |
| Harness code (functions) | A [harness bundle](../reference/bundle-format.md): system prompt, skills, hooks, agents. Claude Code and Codex are closed, so the growable object is the outer layer. |
| Strategy-free scaffold h₀ | An empty bundle, or `harnesses/baseline`. |
| Task stream, K, Q, R_max | `split.train`, `window.size`, `window.min_fixed`, `window.max_attempts`. |
| Function-level trace | The scrubbed optimizer view: trace digest, diff, verifier output, metrics. Hooks and skills as trace components are on the roadmap. |
| Optimizer model (GPT-5.x in the paper) | `optimizer.kind: claude-cli` with any model, `manual`, or a plugin. |
| Edit constraints (L functions, no deletes, no task ids) | `optimizer.max_files`, no deletions, no manifest edits, the leak lint. |
| Gate set G, acceptance SR(h̃) ≥ SR(h) | `split.gate`; a candidate's gate pass rate must not fall below the current version's. |
| Rollback of code, cursor, window and counters | "Current" only moves on accept; the pool and attempt counters are snapshotted per version. |
| Final evaluation | `split.final`: the initial and current version both run on it. |
| LLM calls, deployed-model cost | `llm_calls` and cost metrics; the optimizer's cost is recorded separately, as the paper excludes it. |

## Where Harness Lab is stricter

- Hidden tests never reach the optimizer, enforced by scrubbing and a lint, not only by an
  instruction to the model.
- Every window and gate evaluation is a normal experiment with an independent verifier, base
  commit and reproducibility record.
- Infrastructure failures (a runner outage) never cost a task an attempt.
- Interrupted evaluations are discarded on resume, never accepted.

## Where the paper goes further

- Its harness is open code, so the optimizer can edit control flow. With closed CLIs, Harness Lab
  can only grow prompts, skills, hooks and agents. A plugin runner with its own loop around the
  API would close that gap.
- Its splits are 200 train, 50 gate and 50 final tasks. The bundled suite has three; the loop
  proves the mechanics, not the effect size. A task corpus generator is on the
  [roadmap](../project/roadmap.md).
- Its evaluation domains are web retrieval and browsing; coding agents are untested there. That is
  the experiment Harness Lab now makes possible: a sweep with `harness: {baseline, grown}` crossed
  with `model: {small, large}` on a coding suite.

## Caveats worth carrying

The paper's reported cost excludes the optimizer, the harness language is never named, and no
code was released. Treat its numbers as an existence proof for one domain, and measure your own.

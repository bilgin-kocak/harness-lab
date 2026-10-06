# Roadmap

Nothing below is implemented yet. Items are grouped by what they unlock.

## Measuring better

- **Trace component attribution**: Claude Code hook and skill records as named events, so a failing
  trace says which harness component was active. The paper's ablation says this locality doubles
  the optimizer's effectiveness.
- **Growth-curve chart** on the grow session page.
- **Failure clustering** over normalized traces and verifier output.

## Strategies and adaptation

Measured, not shipped: each of these is compared against a minimal baseline with the paired
statistics already in place.

- **Composite strategies**: cascade (cheap model, quality gate on visible checks, escalate) and
  critique (read-only reviewer from another model family, one revision) runners, so sweeps cover
  `strategy × model` with per-stage cost and escalation rate (after GitHub HydraFusion).
- **Task-class routing policies** learned from sweep results and evaluated on holdout tasks
  against the best global configuration and the per-task oracle; grow history exported as a
  playbook of accepted and rejected edits (after Turbo Harness, arXiv 2609.40330).
- **Research-driven optimizer**: idea cards (source, harness module, mechanism) proposed through
  the grow loop and accepted only with evidence against the minimal baseline (after ScholarEvolve,
  arXiv 2609.40169).
- **Sentinel support, part two**: an action-level dataset exported from traces with hindsight labels,
  and learned (model-based) sentinels compared with the rule-based one now shipped as
  `harnesses/sentinel` (after HiSentinel, arXiv 2609.39957).
- **Improvement tasks beyond the demo**: objectives mined from performance commits, and
  multi-objective improvement (speed and size together) reported as a Pareto front.
- **Workflow-as-code runner**: deterministic steps with narrow LLM judgment nodes, compared against
  an unrestricted agent loop.

## Scaling the corpus

- **Corpus mining beyond Python defaults**: issue-linked prompts (PR and issue text instead of the
  commit message), per-language test-file presets, and a contamination check that flags commits
  older than a model's training cutoff.
- **Verifier generation** and multi-verifier scoring.

## Isolation and platforms

- **Container isolation** (`DockerSandbox`): run harness CLIs inside containers with an executor
  abstraction so hidden tests and the host are unreachable.
- **Native Windows support**.

## Searching smarter

- **Successive halving and Bayesian search** over sweep grids, per-runner concurrency limits,
  cost-aware early stopping.

## Operating

- **Cleanup and resume commands**: delete experiments and sessions, prune artifacts and fixture
  caches, resume interrupted experiments (grow sessions already resume).
- **Live event streaming** to the dashboard while a run executes.
- **Agent causal debugger / delta replay**: replay a trace, locate the first divergence between a
  passing and a failing run, counterfactual interventions on stable event ids.
- More adapters (OpenCode, OpenAI Agents SDK, LangGraph) through `HarnessRunner`, and an open
  control-loop runner so the grow loop can edit control code, not only prompts.

## Not in scope by design

Prompt optimization of task prompts, reinforcement learning, LLM judges, accounts, teams,
billing, distributed workers, Kubernetes, vector databases. Model routing is studied as a measured
strategy (above), not shipped as a production router.

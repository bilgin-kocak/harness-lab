# Roadmap

Nothing below is implemented yet. Items are grouped by what they unlock.

## Measuring better

- **Bootstrap confidence intervals** on the compare view and in the grow gate (task-level cluster
  bootstrap), so decisions on small suites are not made on noise.
- **Trace component attribution**: Claude Code hook and skill records as named events, so a failing
  trace says which harness component was active. The paper's ablation says this locality doubles
  the optimizer's effectiveness.
- **Growth-curve chart** on the grow session page.
- **Failure clustering** over normalized traces and verifier output.

## Scaling the corpus

- **Task corpus generator**: from a real repository's history (commit reverted, prompt from the
  message, verifier from the commit's own tests), so grow sessions and sweeps run on hundreds of
  tasks instead of three.
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
billing, distributed workers, Kubernetes, vector databases, model routing.

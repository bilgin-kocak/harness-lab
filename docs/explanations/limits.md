# Honest limits

What Harness Lab does not do, or does only partly, as of the current version.

## Isolation

Runs are isolated with git worktrees, not containers. Agents run with your privileges and can
reach the host filesystem. Hidden tests can be found on disk by a determined agent; such runs are
flagged, not prevented. See [Security model](security-model.md).

## Platforms

No native Windows: process groups and worktree cleanup are POSIX-only. WSL works.

## Statistics

The compare view shows raw deltas. With repetitions there are no confidence intervals or
significance tests yet, and the grow gate compares plain pass rates. On small suites the gate can
decide on noise. Task-level bootstrap intervals are planned.

## Metrics that depend on the harness

- `reported_cost_usd` exists only when the harness reports cost (Claude Code does, Codex does
  not). Give other harnesses a [pricing table](../reference/pricing.md).
- `llm_calls` is exact for Claude Code and the generic JSONL protocol and `null` for Codex, whose
  stream does not expose model invocations.
- Action granularity cannot be switched inside closed CLIs; `action_policy` steers it through a
  prompt and the realized granularity is measured.

## Growing the harness

- Only the outer harness can be grown for Claude Code and Codex: prompts, skills, hooks, agents.
  The control loop stays closed.
- The bundled demo suite has three tasks, enough to prove the mechanics, not to measure an effect.
- The optimizer sees scrubbed verifier output; assertion messages that print an expected answer
  are not hidden by the scrub. Write hidden tests accordingly.
- No real Claude Code grow session has been run by the maintainers at the time of writing; the
  `claude-cli` optimizer's flags and result format were verified against the installed CLI, and
  the full loop is verified with the fake runner and a replayed CLI.
- Runners read the bundle from its source directory, not from the per-experiment snapshot, so
  editing a bundle while an experiment runs desynchronises hash and content.

## Sweeps

Grids explode; only random subsampling and budgets bound them. Successive halving and Bayesian
search are not implemented. There are no per-runner concurrency limits and no rate-limit handling
beyond counting Claude Code's rate-limit events.

## Dashboard and data

Tables only, no charts. No live streaming while a run executes; refresh the page. No pagination
or filtering on the experiment list. No cleanup or delete commands: experiments, artifacts, kept
worktrees and fixture caches accumulate under `.harnesslab` until you remove the directory.
Schema migrations are forward-only column additions, not a migration tool.

## Scope by design

No prompt optimization of task prompts, no reinforcement learning, no LLM judges, no accounts,
teams, billing, distributed workers, Kubernetes, vector databases or model routing.

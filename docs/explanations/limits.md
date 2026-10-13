# Honest limits

What Harness Lab does not do, or does only partly, as of the current version.

## Isolation

Runs are isolated with git worktrees, not containers. Agents run with your privileges and can
reach the host filesystem. Hidden tests can be found on disk by a determined agent; such runs are
flagged, not prevented. See [Security model](security-model.md).

## Platforms

No native Windows: process groups and worktree cleanup are POSIX-only. WSL works.

## Statistics

Comparisons are paired by task: a task-level bootstrap interval and an exact sign test, with a
verdict only from `--min-tasks` (default 5) paired tasks; repetitions reduce noise within a task
but do not add tasks. The grow gate compares plain pass rates by default (`no_regression`), which
can decide on noise on small suites; `gate.require: not_worse_ci | better_ci` uses the paired
intervals instead. The intervals assume tasks are a sample of the work you care about: on a
hand-picked or mined suite they say how sure the comparison is for those tasks, not for other
projects. See [Read and export results](../guides/results.md#statistics).

## Metrics that depend on the harness

- `reported_cost_usd` exists only when the harness reports cost (Claude Code, pi and OpenCode do,
  Hermes Agent in its session store; Codex does not). Give other harnesses a
  [pricing table](../reference/pricing.md).
- `llm_calls` is exact for Claude Code, pi, OpenCode and the generic JSONL protocol, read from
  Hermes Agent's session store, and `null` for Codex, whose stream does not expose model
  invocations.
- Action granularity cannot be switched inside closed CLIs; `action_policy` steers it through a
  prompt and the realized granularity is measured.

## pi, OpenCode and Hermes Agent

> **Unreleased.** On main; ships in the next release.

- The three adapters were built from each CLI's source and documented output (pi 1.1.0, OpenCode
  1.19.0 and 2.0.27, Hermes Agent v0.21.5) and are tested with constructed streams, not yet with
  a real run. Expect differences; report them.
- None of them resumes a session across improvement rounds (`improve_session: resume` is refused).
- OpenCode reports a tool call only when it finishes: one still running at a timeout leaves no
  trace. The model calls of OpenCode subagents do not appear in its stream, which is why the
  default policy denies them.
- Hermes Agent's stream has no call ids for most tools: calls pair with their results first in,
  first out by tool name, which can mismatch overlapping calls of the same tool. Its cost and model
  calls come from its `state.db`; when that cannot be read they are missing.
- Model calls a harness makes on its own (titles, memory, reviews, approvals by a second model)
  are switched off where Harness Lab knows a switch; others would be invisible in the trace.
- pi, like Claude Code, reads `AGENTS.md` or `CLAUDE.md` files in the directories above the
  worktree. Keep `HARNESSLAB_HOME` outside any project with such files.
- OpenCode's download cache (providers, language servers) is shared with your own OpenCode.

## Growing the harness

- Only the outer harness can be grown for the CLI harnesses: prompts, skills, hooks, agents (as
  far as each harness takes them). The control loop stays closed.
- The bundled demo suite has three tasks, enough to prove the mechanics, not to measure an effect.
- The optimizer sees scrubbed verifier output. By default unittest and pytest assertion details
  are replaced by a placeholder; expected values printed in other ways (custom runners, `print`
  calls, exception messages) are not recognised. Write hidden tests accordingly.
- Optimizers may not write `hooks.json` unless `optimizer.allow_hooks: true`; hook commands run on
  the host outside the agent's tool allowlist, so enable it only on a machine you can throw away.
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

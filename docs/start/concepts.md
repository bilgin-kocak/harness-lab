# Concepts

The central experimental object is

```text
TASK × MODEL × HARNESS × CONFIGURATION × ENVIRONMENT → TRACE → VERIFIER → METRICS
```

| Axis | Harness Lab object | Where it lives |
| --- | --- | --- |
| TASK | a task spec: prompt, fixture repository, verifier, limits | `suites/<suite>/tasks/*.yaml` |
| MODEL | the requested model and the model the harness actually used | variant YAML, `runs` table |
| HARNESS | the runner (`claude`, `codex`, `fake`, `generic`, yours) plus its CLI version, and optionally a harness bundle | variant YAML, `variants` and `runs` tables |
| CONFIGURATION | every other key of a variant, hashed as `config_hash` | variant YAML, `variants` and `runs` tables |
| ENVIRONMENT | sandbox kind, OS, Python, git, hashed as `environment_hash` | `experiments` and `runs` tables |
| TRACE | the normalized event list | `events` table plus a sanitized stream artifact |
| VERIFIER | exit code, optional partial score, protected-path violations | `verifier_results` table |
| METRICS | verified pass and score, wall time, tokens, LLM calls, cost, tool calls, files changed | `runs` table |

## Run, experiment, sweep, grow session

- A **run** is one cell: one task, one variant, one repetition, one environment. It has a status,
  an outcome, a trace, a verdict and metrics.
- An **experiment** is a set of runs: tasks × variants × repetitions, with per-variant aggregates
  and pairwise comparisons. `harnesslab run` creates one.
- A **sweep** is an experiment whose variants were generated from a factor grid, plus a report
  that picks the cheapest configuration that meets a pass-rate requirement.
- A **grow session** is a sequence of experiments driven by an optimizer: each candidate harness
  bundle is evaluated on a window of failures and on a held-out gate, and accepted or rolled back.

## Status versus outcome

A run's **status** is the infrastructure state: `completed`, `timeout`, `crashed`, `unavailable`,
`blocked`, `interrupted`, `skipped`. Its **outcome** is the verifier's verdict: `pass`, `fail`,
`not_verified`. They are independent on purpose. A crashed or timed-out agent is still verified,
because partial work may pass. An unavailable harness is `not_verified` and excluded from pass
rates, so a missing CLI never looks like a failed task.

## Variants and harness bundles

A **variant** names a runner, a model and a configuration. Keys the schema does not know are
passed to the runner verbatim, so new harness knobs never need a schema change. A variant may
also carry a **harness bundle**: a directory with a system prompt, skills, hooks and agents that
the runner applies. The bundle's hash is part of the variant's `config_hash`, so two variants that
differ only by bundle are distinct, and bundles work as sweep factors. See
[Harness bundle](../reference/bundle-format.md).

## The run pipeline

For every cell the experiment service:

1. inserts a `running` run row, so the dashboard shows progress while an experiment runs;
2. snapshots the fixture repository once per experiment and creates a detached git worktree from an
   internal clone, so the source fixture is never modified and has no reachable remote;
3. runs the task's `setup.commands`, which must leave the worktree clean;
4. launches the harness with the worktree as its working directory and an allowlisted environment
   (provider credentials go to the agent only, never to the verifier);
5. captures `git status`, `git diff --stat` and `git diff` against the recorded base commit, which
   stays correct even if the agent committed or reset;
6. runs the verifier in the same worktree: protected paths, then hidden-file injection, then the
   verification command, then the optional partial-score command;
7. computes metrics and persists events, verdict and artifacts;
8. removes the worktree unless `--keep-worktrees` was given.

Runs execute concurrently up to `parallelism`. A failing cell never aborts the experiment.

## Reproducibility

Plain-directory fixtures become a git repository with a fixed author, date and config, so the base
commit SHA is a pure function of the fixture's content and identical on every machine. Every run
records the base commit, task hash, prompt hash, harness bundle hash, configuration hash,
environment hash, Harness Lab version and commit, runner CLI version, and the models requested
and resolved. Experiments export to JSON. See [Read and export results](../guides/results.md).

# Configuration sweeps

Models are increasingly tuned for particular action styles and runtimes. A sweep tests
`model × reasoning effort × toolset × compaction × action policy × harness bundle` (any factors
you like) on a workload and reports the **cheapest configuration that still passes
verification**.

```yaml
# sweeps/claude-config-search.yaml (bundled template; harnesslab init copies it)
name: claude-config-search
suite: demo
base_variant: { runner: claude, max_turns: 30, permission_mode: acceptEdits }
factors:
  model: [claude-haiku-4-5, claude-sonnet-5]
  reasoning_effort: [low, high]                       # --effort
  toolset:
    minimal: { allowed_tools: [Read, Edit, "Bash(python *)"] }
    full:    { allowed_tools: [Read, Edit, Write, MultiEdit, Glob, Grep, LS, "Bash(python *)", "Bash(git diff *)"] }
  compaction:
    default: {}
    tight:   { autocompact: 100k }                     # --autocompact
  action_granularity:
    batched: { action_policy: batched }                # appended system prompt
    fine:    { action_policy: fine }
repetitions: 2                 # "verified" means the pass-rate requirement holds over repetitions
workload_by: suite             # suite | task | tag  -> one recommendation per workload
holdout_tasks: [consolidate-money-formatting]   # selected on the other tasks, reported on these
sample: { max_configs: 8, seed: 7 }             # random subset of the 32-configuration grid
budget: { max_runs: 60, max_cost_usd: 15 }      # remaining runs are recorded as "skipped"
objective:
  require: { min_pass_rate: 1.0 }               # also min_valid_runs (default: every planned run)
  minimize: cost               # cost | tokens | wall_time | llm_calls
  tie_breaker: wall_time_seconds
```

A factor is either a list of scalar levels (each level sets the option named like the factor) or a
mapping of level name to option overrides. Every configuration is `base_variant` with one level
of each factor merged over it, named `factor=level|factor=level|…`.

```bash
harnesslab sweep run sweeps/demo-fake.yaml           # fake runner: seconds, no credentials
harnesslab sweep run sweeps/claude-config-search.yaml --dry-run   # print the expanded grid
harnesslab sweep run sweeps/claude-config-search.yaml             # real API usage!
harnesslab sweep report <experiment-id>              # recompute from the database
```

## The report

Per workload, in the terminal, the dashboard and the JSON export:

- the **recommended** configuration: the eligible one with the lowest median objective, ties
  broken by `tie_breaker`;
- the runner-up, and the recommended configuration's pass rate on the holdout tasks;
- every configuration with its eligibility reason (`no verified runs`, `pass rate 50% below 100%`,
  `only 3 of 6 required valid runs`, …);
- the pass-rate versus cost **Pareto front**;
- **factor effects**: the marginal mean pass rate and median objective per factor level, which is
  the component-ablation view;
- **per-task selection** *(unreleased)*: whether choosing a configuration per task would beat the
  best single one (see below).

A configuration is *eligible* when its verified pass rate over valid runs meets
`require.min_pass_rate` and it has at least `require.min_valid_runs` valid runs. The objective for
`cost` is the harness-reported cost when any run reported one, else the estimate from your
[pricing table](../reference/pricing.md), else total tokens, and the report says which was used.

The recommendation is also compared, task by task, with the runner-up and with the `baseline`
configuration, using the paired analysis in [Read and export results](results.md#statistics).
*(unreleased)* Those verdicts are about `verdict_metric`: `pass_rate` by default, `score` for
tasks with a partial score and for [improvement tasks](improvement.md), or `improve_ratio` (only
tasks where both sides have a ratio). The report header names the metric; eligibility and the
ranking are not affected by it.

A sweep is an ordinary experiment whose variants carry `factors`, so `experiment show`, the
compare view and the export all work on it.

## Would choosing per task help?

> **Unreleased.** On `main`; ships in the next release.

A sweep recommends one configuration per workload. Some work suggests that building the harness
per query beats the best fixed harness. The runs a sweep already has answer whether that holds for
your tasks, without running anything new. Each workload's report ends with one line:

```text
per-task selection: best single model=claude-sonnet-5|… 62% · best per task 78% (+16 points, 5 of 12 tasks) · held out +4 points
```

Only valid runs count, and only tasks on which every configuration has at least one valid run;
the line says how many tasks were left out.

- **best single** is the configuration with the highest mean per-task pass rate (ties go to the
  configuration listed first).
- **best per task** picks, after seeing the results, the configuration that did best on each task
  and averages those per-task pass rates. The difference to the best single configuration is the
  in-sample gap, followed by how many tasks some configuration does better on.
- **held out** is the same comparison made honestly. Each configuration's runs on a task are split
  by repetition into even and odd. The per-task choice and the best single configuration are
  picked on one half and both are scored on the other half, in both directions, and the two
  differences are averaged. A task's choice stays with the best single configuration unless
  another one did strictly better on that half.

Trust the held-out number. The in-sample gap takes a maximum over several configurations per task,
and with few repetitions that maximum is high by chance: identical configurations with coin-flip
outcomes show a large in-sample gap and a held-out gain near zero. Only a held-out gain well above
zero, over many tasks, says that per-task selection is worth building. It needs at least two
repetitions; with one, the line says `held out: needs at least 2 repetitions per variant`. With
`workload_by: task` each workload has one task, so the gap is always zero; use `suite` or `tag`.

`experiment show` prints the same line for any experiment with two or more variants, the
dashboard's experiment page shows it as a panel, and the export has it under `routing` (per sweep
workload under `sweep_report.workloads[].routing`).

## Harness bundles as a factor

Because a bundle's hash is part of the configuration hash, bundles are just another factor:

```yaml
factors:
  harness:
    baseline: { harness: ../harnesses/baseline }
    grown:    { harness: ../harnesses/grown }
  model: [claude-haiku-4-5, claude-sonnet-5]
```

This is the experiment behind the Growing Harness paper's headline table: does a grown harness
let a small model match a large one on verified pass rate at a fraction of the calls? See
[Grow the harness](growing.md).

## Honest limits

- **Action granularity** cannot be switched inside closed CLI harnesses. `action_policy` steers it
  through an appended system prompt (Claude Code) or a prompt prefix (Codex), and the *realized*
  granularity is measured per run: `tool_calls_per_turn`, `mean_command_chars`,
  `edits_per_changed_file`.
- Cost is only reported by Claude Code. Give Codex or custom harnesses a pricing table;
  otherwise sweeps rank by total tokens and say so.
- Grids explode: use `sample`, `budget`, `tasks:` and `holdout_tasks`. Successive halving and
  Bayesian search are on the [roadmap](../project/roadmap.md).

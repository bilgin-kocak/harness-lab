# Sweep YAML

`harnesslab sweep run <sweep.yaml|bundled-name>`. Bundled templates: `demo-fake`,
`claude-config-search`. See [Configuration sweeps](../guides/sweeps.md) for the semantics.

```yaml
name: claude-config-search
suite: demo
description: ...
plugins: []
base_variant: { runner: claude, max_turns: 30 }      # required; must name a runner
factors:                                             # required; at least one factor with levels
  model: [claude-haiku-4-5, claude-sonnet-5]         # scalar levels set the option named like the factor
  toolset:                                           # mapping levels set arbitrary options
    minimal: { allowed_tools: [Read, Edit, "Bash(python *)"] }
    full: { allowed_tools: [Read, Edit, Write, Glob, Grep, "Bash(python *)"] }
  harness:
    baseline: { harness: ../harnesses/baseline }
    grown: { harness: ../harnesses/grown }
repetitions: 2
parallelism: 1
tasks: null                                          # optional subset of task ids
workload_by: suite                                   # suite | task | tag
holdout_tasks: []                                    # excluded from selection, reported separately
sample: { max_configs: 8, seed: 7 }                  # optional random subset of the grid
budget: { max_runs: 60, max_cost_usd: 15 }           # optional; remaining runs are recorded as skipped
objective:
  require: { min_pass_rate: 1.0, min_valid_runs: null }   # null = every planned run must be valid
  minimize: cost                                     # cost | tokens | wall_time | llm_calls
  tie_breaker: wall_time_seconds                     # wall_time_seconds | tokens | cost | llm_calls
keep_worktrees: false
```

| Field | Default | Meaning |
| --- | --- | --- |
| `base_variant` | required | Merged under every configuration. Keys other than `runner` and `model` are runner options. |
| `factors` | required | Name → list of scalar levels, or name → `{level: {option: value}}`. The grid is the cartesian product. Configurations are named `factor=level\|…`. |
| `repetitions` | `2` | "Verified" means the requirement holds over repetitions. |
| `workload_by` | `suite` | One recommendation per suite, per task, or per tag. |
| `holdout_tasks` | `[]` | The recommended configuration's pass rate on these is reported, never used for selection. |
| `sample.max_configs`, `sample.seed` | none | Deterministic random subset of the grid. |
| `budget.max_runs`, `budget.max_cost_usd` | none | Runs beyond the budget are recorded with status `skipped`. Cost counts reported cost, else estimated cost. |
| `objective.minimize` | `cost` | `cost` uses reported cost, else the pricing estimate, else total tokens, and the report names which. |
| `objective.require.min_pass_rate` | `1.0` | Over valid runs. |
| `objective.require.min_valid_runs` | planned runs | Fewer valid runs make a configuration ineligible. |

The sweep runs as one experiment whose variants carry `factors`; the experiment record stores the
sweep spec, so `harnesslab sweep report <experiment-id>` can recompute the report at any time.

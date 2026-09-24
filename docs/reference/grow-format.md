# Grow YAML

`harnesslab grow run <grow.yaml|bundled-name>`. Bundled templates: `demo-fake`, `claude-grow`.
See [Grow the harness](../guides/growing.md) for the loop.

```yaml
name: claude-grow                          # required
suite: demo                                # required; path relative to this file or bundled name
description: ...
plugins: []
base_variant: { runner: claude, model: claude-haiku-4-5, max_turns: 30 }   # required; must name a runner
harness: ../harnesses/baseline             # initial bundle directory; omit for an empty bundle
split:
  train: [fix-month-boundary, add-tag-budgets]   # explicit lists...
  gate: [consolidate-money-formatting]
  final: []
  # ...or fractions with a seed (shuffled, then sliced train / gate / final):
  # fractions: { train: 0.6, gate: 0.2, final: 0.2 }
  # seed: 7
window:
  size: 4                                  # K: failing tasks per window (>= 1)
  min_fixed: 1                             # Q: fixes required to reach the gate (1..size)
  max_attempts: 5                          # R_max: a task retires after this many failed attempts
repetitions: 1
parallelism: 1
max_iterations: 10
optimizer:
  kind: claude-cli                         # claude-cli | manual | fake | a plugin name
  model: claude-sonnet-5                   # passed to the optimizer
  max_files: 6                             # files a candidate may change
  # any other key is an optimizer option (e.g. executable, timeout_seconds, extra_args)
budget:
  max_runs: 200                            # deployed runs across the whole session
  max_cost_usd: 20                         # deployed cost (reported, else estimated)
  max_optimizer_cost_usd: 10               # the optimizer's own cost
report:
  minimize: llm_calls                      # llm_calls | cost | tokens | wall_time (shown, not selected on)
keep_worktrees: false
```

Validation rules:

- `split.train` and `split.gate` must be non-empty, contain only task ids from the suite, and not
  overlap; with `fractions`, the suite needs at least two tasks and the fractions sum to at most 1.
- `window.min_fixed` must not exceed `window.size`.
- `optimizer.kind` must be a registered optimizer.
- `base_variant` may carry `gate_overrides: {runner, model, ...}`: settings applied only to gate
  evaluations (advanced; used to evaluate the gate under different conditions).

## Session state

Everything the loop needs to continue is persisted after every step: the phase
(`baseline_gate`, `baseline_train`, `iterating`, `final`, `done`), the iteration, the failure pool,
attempts and retired tasks, the current version, the candidate under evaluation, budget counters
and recent rejection reasons. `harnesslab grow resume <session>` reloads it; a candidate whose
evaluation was interrupted is marked `discarded` and the iteration restarts.

## Version statuses

| Status | Meaning |
| --- | --- |
| `initial` | Version 0, the starting bundle. |
| `candidate` | Proposed and under evaluation. |
| `invalid` | Failed validation or the leak lint, or the optimizer errored. Costs the window tasks an attempt. |
| `rejected` | Fixed fewer than Q window tasks, or regressed the gate. Costs an attempt. |
| `accepted` | Became the current version. |
| `discarded` | Its evaluation was interrupted; never accepted. |

Three consecutive optimizer failures (errors or invalid candidates) fail the session.

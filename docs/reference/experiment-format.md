# Experiment YAML

An experiment file fixes the variants, repetitions and parallelism for a suite so a comparison
can be re-run exactly. `harnesslab run <experiment.yaml>` accepts it wherever a suite is accepted;
a file with a `suite:` key and no `tasks:` key is treated as an experiment.

```yaml
# suites/demo/experiments/baseline.yaml
name: baseline-comparison                 # required; the experiment's name in the database
suite: ../suite.yaml                      # required; a path relative to this file or a bundled name
description: Compare the reference fake agent against a no-op control.
repetitions: 3                            # default 1
parallelism: 2                            # default 1; runs executing concurrently
tasks: [fix-month-boundary]               # optional subset of the suite's task ids
keep_worktrees: false                     # keep every run's worktree for inspection
plugins: []                               # python modules to import first
variants:
  - id: fake-reference
    runner: fake
    behavior: solve
  - id: fake-noop
    runner: fake
    behavior: noop
```

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Experiment name. `--name` overrides it. |
| `suite` | required | Suite path or bundled name. |
| `description` | none | Free text. |
| `repetitions` | `1` | Runs per task × variant cell. `--repetitions` overrides. |
| `parallelism` | `1` | Concurrent runs. `--parallelism` overrides. |
| `tasks` | all | Task ids to run. `--tasks` overrides. |
| `keep_worktrees` | `false` | `--keep-worktrees` sets it. |
| `plugins` | `[]` | Imported together with the suite's plugins and `--plugin` values. |
| `variants` | `[]` | Same schema as suite variants; take precedence over suite variants with the same id. |

Variant resolution for `--variants a,b`: experiment variants, then suite variants, then the
built-ins. With no `--variants`, the experiment's variants run; if it has none, the suite's; if
neither has any, `fake-reference` runs.

Bundle paths in `harness:` are resolved relative to the experiment file.

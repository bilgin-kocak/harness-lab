# Read and export results

## In the terminal

```bash
harnesslab experiment list
harnesslab experiment show <experiment-id>        # matrix, aggregates, every run
harnesslab sweep report <experiment-id>           # recompute a sweep's recommendation
harnesslab experiment compare <experiment-id> <a> <b>   # paired evidence: B vs A per task
harnesslab ablate report <experiment-id>          # component verdicts of an ablation
harnesslab grow list
harnesslab grow show <session-id>                 # version lineage and gate results
```

Ids accept a unique prefix or the exact name.

## Export an experiment

```bash
harnesslab experiment export <experiment-id> > experiment.json
harnesslab experiment export <experiment-id> --no-events --no-artifacts -o summary.json
```

The document contains:

- `experiment`: name, suite, status, timestamps, Harness Lab version and commit, environment and
  its hash, the spec that produced it;
- `variants`: id, runner, model, description, configuration and `config_hash`, sweep factors,
  `harness_hash` and the bundle's file list;
- `tasks`: id, name, version, `task_hash`, `spec_hash`, `prompt_hash`, base commit, tags;
- `runs`: one entry per run with status, outcome, timestamps, a `reproducibility` block (base
  commit, hashes, runner, configuration, models requested and resolved, CLI version), the full
  `metrics`, the verifier result, and, unless disabled, the normalized events and the text
  artifacts (diff, verifier output) inline;
- `aggregates`: per-variant statistics (see [Metrics](../reference/metrics.md));
- `sweep_report`: the recommendation report when the experiment was a sweep, including paired
  evidence against the runner-up and the baseline;
- `ablation_report`: per-component verdicts when the experiment was an ablation.

The same document is served at `/api/experiments/{id}/export.json`. It is plain JSON with no
NaN values, so it loads with any tool.

## Export a grow session

```bash
harnesslab grow export <session-id> harnesses/grown
```

writes the session's current accepted bundle into the directory plus `lineage.json`, which lists
every version with its status, reason, window tasks and fixes, gate pass rate, median `llm_calls`
and cost, optimizer model and cost, and rationale. The bundle is ready to be used as
`harness:` on any variant.

## Analyse in Python

```python
import json
import pandas as pd

doc = json.load(open("experiment.json"))
runs = pd.json_normalize(doc["runs"])
runs.groupby("variant")[["metrics.verified_pass", "metrics.llm_calls", "metrics.wall_time_seconds"]].mean()
```

`aggregates` already holds per-variant means, medians, standard deviations, minima and maxima,
and per-task pass rates, so most comparisons need no computation.

## Statistics

Every decision Harness Lab supports (compare view, sweep recommendations, ablations, the optional
statistical grow gate) uses the same paired analysis, in `harnesslab.experiments.stats`:

1. **Pair by task.** Both sides ran the same tasks from the same base commit. Within a task,
   repetitions are averaged first, so a task with five repetitions counts once. Tasks without a
   verified run on both sides are left out.
2. **Per-task differences** of pass rate (B − A), of cost (reported, else estimated, else total
   tokens) and of `llm_calls`.
3. **Cluster bootstrap over tasks**: 2,000 seeded resamples of the tasks, percentile 95% interval
   of the mean difference, and P(> 0), the share of resampled means above zero.
4. **Exact sign test** on tasks where one side did better (ties dropped), as a check that does not
   depend on resampling.
5. **Verdict**: `better` or `worse` only when the interval excludes zero *and* at least 5 tasks
   (configurable with `--min-tasks`) were paired; otherwise `no evidence` or `not enough tasks`.

The intervals are honest about small suites: with the three-task demo suite they are wide and no
verdict is given. Use them to decide whether a difference is worth acting on, and add tasks before
trusting a close call.

### Verdict metric

> **Unreleased.** On `main`; ships in the next release.

The verdict, the wins/losses/ties and the sign test are about one metric, chosen per comparison:

| Metric | Per-task value | Tasks counted |
| --- | --- | --- |
| `pass_rate` (default) | Share of valid runs that passed. | Every paired task. |
| `score` | Mean `verified_score` over the task's valid runs. | Tasks where both sides have a score. |
| `improve_ratio` | Mean `improve_ratio` over the task's valid runs. | Tasks where both sides have a ratio. |

The interval of the chosen metric decides the verdict, and `--min-tasks` applies to the tasks
counted for it, not to every paired task. The other metrics are still shown, with a score row
next to pass rate, cost and `llm_calls` (and the improvement ratio when there is one).

Pass rate is the right metric for pass/fail tasks. Use `score` when tasks have a partial-score
command, and for [improvement tasks](improvement.md): there `verified_score` is `1 − 1/ratio`, `0`
when the final state fails and `1` when the objective was minimized to zero, so every run counts.
`improve_ratio` only uses tasks where both sides have a ratio. A run whose final state broke has
none and is left out of its task's mean, and a task where one side never kept a working final
state is not counted at all, so breaking the code is never held against that side.

```bash
harnesslab experiment compare <experiment-id> <a> <b> --metric score
```

The compare view has the same choice (links above the evidence table, or `?metric=score`); sweeps
set it with `verdict_metric` ([sweep format](../reference/sweep-format.md)), ablations with
`ablate run --metric` ([ablation](ablation.md)) and the grow gate with `gate.metric`
([grow format](../reference/grow-format.md)).

## Reading a single run

Statuses and outcomes are independent (see [Concepts](../start/concepts.md)). When a run looks
wrong, read in this order: the verifier's stderr (why it failed), the diff (what the agent
changed), the timeline (what it did), then the runner metadata in the reproducibility record
(permission denials, API retries, unknown records in the stream). The `possible_suite_access`
flag in the metadata marks runs whose shell commands mentioned the suite directory.

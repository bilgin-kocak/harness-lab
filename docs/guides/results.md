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

## Reading a single run

Statuses and outcomes are independent (see [Concepts](../start/concepts.md)). When a run looks
wrong, read in this order: the verifier's stderr (why it failed), the diff (what the agent
changed), the timeline (what it did), then the runner metadata in the reproducibility record
(permission denials, API retries, unknown records in the stream). The `possible_suite_access`
flag in the metadata marks runs whose shell commands mentioned the suite directory.

# Read and export results

## In the terminal

```bash
harnesslab experiment list
harnesslab experiment show <experiment-id>        # matrix, aggregates, every run
harnesslab sweep report <experiment-id>           # recompute a sweep's recommendation
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
- `sweep_report`: the recommendation report when the experiment was a sweep.

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

## Reading a single run

Statuses and outcomes are independent (see [Concepts](../start/concepts.md)). When a run looks
wrong, read in this order: the verifier's stderr (why it failed), the diff (what the agent
changed), the timeline (what it did), then the runner metadata in the reproducibility record
(permission denials, API retries, unknown records in the stream). The `possible_suite_access`
flag in the metadata marks runs whose shell commands mentioned the suite directory.

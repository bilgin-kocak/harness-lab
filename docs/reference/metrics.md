# Metrics

## Per run

Stored in the `runs` table as columns and as `metrics_json`, shown on the run page, exported under
`metrics`.

| Metric | Meaning |
| --- | --- |
| `verified_pass` | Exit code 0 of the verification command. `null` when the verifier could not run (harness unavailable, injection failed). |
| `visible_pass` *(unreleased)* | Exit code 0 of the task's `visible_command`, run on a scratch copy of the worktree before the hidden files are injected. `false` when the run edited protected paths; `null` without a visible command or when the check could not run. |
| `verified_score` | `score / max_score` from the partial-score command if there is one, the chosen F1 with an [answer key](../guides/discovery.md), else `1.0` / `0.0`. |
| `task_metrics` *(unreleased)* | The numeric metrics the task's own checks reported: the score command's `metrics`, or the answer key's F1 scores and counts. Booleans count as 0 and 1. |
| `wall_time_seconds` | The whole pipeline: agent, capture and verifier. Also `agent_wall_time_seconds` and `verifier_wall_time_seconds`. |
| `input_tokens` | *Uncached* prompt tokens. Codex reports cached tokens inside its input count and Claude Code reports them separately; adapters normalize both to this shape. |
| `cached_input_tokens`, `cache_write_tokens` | Cache reads and cache writes. |
| `output_tokens`, `reasoning_output_tokens`, `total_tokens` | Output, reasoning output where reported, and the sum of input, cached, cache-write and output tokens. |
| `llm_calls` | Model invocations: distinct assistant messages for Claude Code, `usage` events for the generic JSONL protocol, `fake.yaml` or a per-behaviour default for the fake runner, `null` for Codex. |
| `reported_cost_usd` | What the harness itself reported (Claude Code does, Codex does not). Never invented. |
| `estimated_cost_usd`, `pricing_version` | Computed only from your [pricing table](pricing.md); the version keeps estimates attributable. |
| `tool_calls`, `shell_commands`, `tool_calls_unfinished`, `subagent_tool_calls` | Counted from normalized events. |
| `assistant_messages`, `reasoning_events`, `error_events`, `file_change_events`, `events_total` | Event counts. |
| `files_changed`, `lines_added`, `lines_deleted` | From `git diff --numstat` against the base commit. |
| `agent_exit_code`, `verifier_exit_code`, `num_turns`, `permission_denials` | Raw process facts. |
| `tool_calls_per_turn`, `mean_command_chars`, `edits_per_changed_file` | Realized action granularity, measured regardless of any requested action policy. |
| `improve_baseline`, `improve_best`, `improve_final`, `improve_ratio`, `improve_progress`, `improve_rounds`, `improve_curve`, `improve_history`, `evaluator_calls`, `improve_evaluations`, `improve_evaluation_curve`, `improve_anytime` | Improvement tasks only; see [Improvement tasks](../guides/improvement.md#reading-the-results). For them `verified_pass` means "passed the gate and beat the baseline" and `verified_score` is `1 − 1/ratio` (`1` for minimizing to 0). |
| `safe`, `safety_violations`, `risky_actions`, `risky_blocked`, `hook_blocks`, `safety_counts` | Every run that reached the agent; see [Safety](../guides/safety.md#reading-the-results). |

`metrics_version` records the version of these definitions.

## Status and outcome

Run **status** is the infrastructure state: `completed`, `timeout`, `crashed`, `unavailable`,
`blocked`, `interrupted`, `skipped`. **Outcome** is the verifier's verdict: `pass`, `fail`,
`not_verified`. A crashed or timed-out agent is still verified; an unavailable harness is
`not_verified` and excluded from pass rates. A run is *valid* when it has a verdict.

## Per variant

Shown on the experiment page and by `experiment show`, exported under `aggregates`.

| Aggregate | Meaning |
| --- | --- |
| `n_total`, `n_valid`, `n_passed`, `n_failed`, `n_not_verified`, `n_infra_failures`, `n_skipped` | Counts. Infrastructure failures are `timeout`, `crashed`, `unavailable`, `blocked`, `interrupted`. |
| `success_rate` | `n_passed / n_valid`. |
| `score`, `wall_time_seconds`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `tool_calls`, `llm_calls`, `shell_commands`, `files_changed`, `reported_cost_usd`, `estimated_cost_usd` | Each a `Stat`: `n`, `mean`, `median`, `std`, `min`, `max`. |
| `per_task_pass_rate` | Task key → pass rate over valid runs. |
| `improve_ratio`, `evaluator_calls`, `n_improved` | Improvement tasks: ratio over runs whose final state passed verification, in-loop measurements, runs that passed by improving (with or without a finite ratio). |
| `improve_anytime`, `improve_evaluations` *(unreleased)* | Improvement tasks: the anytime score and the objective evaluations, each a `Stat` over valid runs. |
| `task_metrics` *(unreleased)* | Metric name → `Stat` over the valid runs that reported it. |
| `safe_rate`, `n_safe`, `safe_pass_rate`, `risky_actions`, `safety_violations` | Share of runs with no executed high-severity finding, how many, and the share of valid runs that passed *and* were safe. |

With repetitions the matrix shows `k/n` per cell and every individual run stays listed. Averages
never hide runs.

## Comparison

The compare view puts two variants side by side (pass rate, score, tokens, LLM calls, wall time,
tool calls, cost, each with the delta and a "lower is better" marker) and classifies each task as
*both passed*, *both failed*, *A only*, *B only*, *mixed* (repetitions disagree) or *unverified*.
There is deliberately no composite "winner" score.

### Paired verdicts

> **Unreleased.** On `main`; ships in the next release.

Below the side-by-side table, the paired evidence (see [Statistics](../guides/results.md#statistics))
shows per-task differences of pass rate, score, cost, `llm_calls` and, for improvement tasks, the
improvement ratio and anytime score, each with a bootstrap interval. The verdict is about one of
them, the *verdict metric*: `pass_rate` (default), `score`, `improve_ratio` or `anytime`
*(unreleased)*. Its interval decides the verdict; its
per-task differences give wins, losses, ties and the sign test; and `min_tasks` counts the tasks
where both sides have it. `PairedComparison` records the choice as `metric`, the count as
`n_metric_tasks` (next to `n_tasks`, every paired task) and the score and anytime intervals as
`score_diff` and `anytime_diff`.

Use `score` for improvement tasks: it counts a broken final state as 0 and an objective minimized
to 0 as 1, which the ratio cannot. `improve_ratio` only uses tasks where both sides have a ratio.
Choose the metric with `experiment compare --metric`, `?metric=` on the compare view, `ablate run
--metric`, `verdict_metric` in a sweep and `gate.metric` in a grow spec.

## Sweep and grow reports

A sweep's `ConfigResult` carries `pass_rate`, `n_valid`, the median objective, `median_tokens`,
`median_wall_time`, `median_tool_calls`, `median_llm_calls`, `median_files_changed`, eligibility
and reason. A grow session's version report carries the window tasks and fixes, the gate pass
rate and counts, the median gate `llm_calls` and cost, and the optimizer's cost.

## Per-task selection

> **Unreleased.** On `main`; ships in the next release.

`routing`, per experiment or sweep workload with two or more variants and tasks:
`best_single` and its mean per-task pass rate `best_single_rate`, `oracle_rate` (the best variant's
pass rate on each task, averaged; in sample), `gap` (`oracle_rate − best_single_rate`),
`n_improvable` (tasks some variant does better on), `per_task_best`, and `held_out_gain` over
`n_held_out_tasks` (choices made on even repetitions and scored on odd ones, and back). It covers
the `n_tasks` tasks every variant has valid runs on, with `n_excluded` left out; see
[Would choosing per task help?](../guides/sweeps.md#would-choosing-per-task-help).

## Several attempts

> **Unreleased.** On `main`; ships in the next release.

`attempts`, per variant over its repeated attempts: `k_max` (the lower median number of verified
attempts per task), `n_tasks` (tasks with at least `k_max` of them) and `n_short_tasks` (tasks
left out with fewer), `rows` with `pass_at_k` and `best_of_k` for k = 1 to `k_max` (averaged over
the counted tasks),
`selector_gain` (best-of-`k_max` minus pass@1, bootstrap interval over tasks) with its `verdict`,
`n_best_tasks` (counted tasks whose attempts all have a `visible_pass`) and `cost_per_attempt`;
see [Several attempts](../guides/results.md#several-attempts-passk-and-best-of-k).

## Recovery

> **Unreleased.** On `main`; ships in the next release.

`recovery`, per variant, for suites whose tasks name a fault-free twin with `fault_of`:
`n_pairs` (task pairs with valid runs on both sides), `nominal_success` and `fault_success` (mean
pass rates of the fault-free tasks and their twins), `conditional_recovery` (passes with the
fault among the `n_conditioned` repetitions that passed without it, repetition *r* paired with
repetition *r*) and `duplicate_effects` (the mean `duplicates` task metric of the runs with a
fault, when their checks report one); see [Recovery tasks](../guides/recovery.md).

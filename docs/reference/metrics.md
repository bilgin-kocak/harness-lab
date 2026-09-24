# Metrics

## Per run

Stored in the `runs` table as columns and as `metrics_json`, shown on the run page, exported under
`metrics`.

| Metric | Meaning |
| --- | --- |
| `verified_pass` | Exit code 0 of the verification command. `null` when the verifier could not run (harness unavailable, injection failed). |
| `verified_score` | `score / max_score` from the partial-score command if there is one, else `1.0` / `0.0`. |
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

With repetitions the matrix shows `k/n` per cell and every individual run stays listed. Averages
never hide runs.

## Comparison

The compare view puts two variants side by side (pass rate, score, tokens, LLM calls, wall time,
tool calls, cost, each with the delta and a "lower is better" marker) and classifies each task as
*both passed*, *both failed*, *A only*, *B only*, *mixed* (repetitions disagree) or *unverified*.
There is deliberately no composite "winner" score.

## Sweep and grow reports

A sweep's `ConfigResult` carries `pass_rate`, `n_valid`, the median objective, `median_tokens`,
`median_wall_time`, `median_tool_calls`, `median_llm_calls`, `median_files_changed`, eligibility
and reason. A grow session's version report carries the window tasks and fixes, the gate pass
rate and counts, the median gate `llm_calls` and cost, and the optimizer's cost.

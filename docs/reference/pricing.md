# Pricing table

Harness Lab never hard-codes API prices. `reported_cost_usd` is whatever the harness itself
reported (Claude Code reports one, the Codex CLI does not). `estimated_cost_usd` is computed only
when you provide a pricing table, and every estimate carries the table's `version` so it stays
attributable.

## Where it is found

In this order: `--pricing path/to/pricing.yaml` on `run`, `sweep run` or `grow run`; then
`pricing.yaml` in the Harness Lab home directory; then `pricing.yaml` in the current directory.
`harnesslab init` copies `pricing.example.yaml`; copy it to `pricing.yaml` and fill in the rates
from your provider's current price list. The example's numbers are placeholders, not prices.

## Format

```yaml
version: "2026-09-my-team"      # any string; stored with every estimate (defaults to a content hash)
currency: USD
models:
  claude-sonnet-5:              # exact model id first; otherwise the longest matching prefix wins,
    input_per_million: 3.0      #   so "claude-sonnet-5" also covers "claude-sonnet-5-20260101"
    cached_input_per_million: 0.3
    cache_write_per_million: 3.75
    output_per_million: 15.0
  gpt-5-codex:
    input_per_million: 1.25
    cached_input_per_million: 0.125
    output_per_million: 10.0
```

| Rate | Applied to | Fallback |
| --- | --- | --- |
| `input_per_million` | uncached input tokens | required (defaults to 0) |
| `cached_input_per_million` | cache-read input tokens | `input_per_million` |
| `cache_write_per_million` | cache-creation input tokens | `input_per_million` |
| `output_per_million` | output tokens | required (defaults to 0) |
| `reasoning_output_per_million` | reserved | not used yet |

## How an estimate is computed

When a run reports usage per model (Claude Code does), each model's usage is priced with its own
entry and summed; if any model has no entry the estimate is `null` rather than partial. Otherwise
the run's resolved model and total usage are priced. Estimates are rounded to six decimals.

Sweeps and grow budgets use the reported cost when a run has one and the estimate otherwise;
with neither, sweeps rank by total tokens and say so in the report.

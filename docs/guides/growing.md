# Grow the harness

Harness Lab can run the loop from *Grow the Harness, Not the Context* (Li et al., 2026,
arXiv 2609.26760) for coding agents: failures drive edits to a **harness bundle**, a held-out
**gate** rejects edits that regress, accepted versions accumulate, and the growth curve is
recorded with the same reproducibility guarantees as every other run. The mapping to the paper
is in [The Growing Harness paper](../explanations/growing-harness-paper.md).

## The harness bundle

A bundle is a plain directory, git-trackable and hand-editable, that any variant can carry with
`harness: path/to/bundle`:

```text
harnesses/baseline/
  harness.yaml         optional: name, description
  system_prompt.md     appended to the agent's system prompt
  skills/<name>/SKILL.md
  hooks.json           Claude Code hooks (plugin format)
  agents/<name>.md
  fake.yaml            simulation only: solve_tasks, fail_tasks, llm_calls (fake runner)
```

Claude Code and Codex are closed, so the bundle is the *outer* harness. The Claude adapter passes
`system_prompt.md` with `--append-system-prompt-file` and materializes skills, hooks and agents as
a plugin loaded with `--plugin-dir`. Codex gets the system prompt and skills as a prompt prefix
(hooks and agents are recorded as ignored). The generic runner gets `{harness_dir}` and
`HARNESSLAB_HARNESS_DIR`. Every run records `harness_hash`, and the hash is part of
`config_hash`. Details in [Harness bundle](../reference/bundle-format.md).

## Run a session

```bash
harnesslab grow run demo-fake                       # fake runner + fake optimizer: seconds, no keys
harnesslab grow run grow/claude-grow.yaml --dry-run # print split, window, first optimizer view
harnesslab grow run grow/claude-grow.yaml           # Claude Code deployed, Claude optimizer: real API usage
harnesslab grow show <session-id>                   # version lineage, gate pass rates, llm_calls
harnesslab grow export <session-id> harnesses/grown # copy the current bundle + lineage.json
harnesslab grow resume <session-id>                 # continue after Ctrl-C or a crash
harnesslab harness check harnesses/grown --suite demo   # validate + leak lint
```

A grow spec (`harnesslab init` copies two templates into `grow/`):

```yaml
name: claude-grow
suite: demo
base_variant: { runner: claude, model: claude-haiku-4-5, max_turns: 30 }
harness: ../harnesses/baseline           # initial bundle; omit for an empty one
split:                                   # explicit lists, or fractions: {train: .6, gate: .2, final: .2} + seed
  train: [fix-month-boundary, add-tag-budgets]
  gate:  [consolidate-money-formatting]
window: { size: 2, min_fixed: 1, max_attempts: 3 }    # K, Q, R_max from the paper
optimizer: { kind: claude-cli, model: claude-sonnet-5, max_files: 4 }
budget: { max_runs: 40, max_cost_usd: 10, max_optimizer_cost_usd: 5 }
report: { minimize: llm_calls }
```

Every field is listed in [Grow YAML](../reference/grow-format.md).

## What happens in an iteration

1. **Baseline** (once): the gate set runs on the initial bundle to get its gate pass rate, and the
   train set runs to build the failure pool.
2. **Window**: up to K failing train tasks, fewest attempts first.
3. **Proposal**: the optimizer sees the current bundle, the window's failures and the constraints,
   and proposes a candidate. Invalid or leaking candidates are recorded as `invalid` and cost an
   attempt.
4. **Window check**: the window runs against the candidate. A task counts as fixed only if every
   repetition passes. Fewer than Q fixed: `rejected`.
5. **Gate check**: the gate set runs against the candidate. Pass rate below the current version's,
   or no valid runs: `rejected`.
6. **Accept**: the candidate becomes current, fixed tasks leave the pool, unfixed ones gain an
   attempt, tasks at `max_attempts` retire. Runs that were `not_verified` because of an
   infrastructure failure never cost an attempt.

Rollback is implicit: "current" only moves on accept, and the pool and attempt counters are
snapshotted per version. The loop stops on `max_iterations`, an empty pool or a budget. If
`split.final` names tasks, the initial and the current version both run on them, which yields the
paper's Table 1 comparison for your suite.

Every window and gate evaluation is an ordinary experiment, tagged with its role in the dashboard.
Each version's bundle, the exact optimizer view (`context.json`) and the raw proposal live under
`.harnesslab/grow/<session>/v<N>/`, so any decision can be audited. `grow resume` continues from
the saved state and discards a candidate whose evaluation was interrupted rather than accepting it.

## Optimizers

Optimizers are plugins (`harnesslab.grow.optimizers.base.Optimizer`, entry-point group
`harnesslab.optimizers`). Built in:

- **claude-cli**: the Claude Code binary in print mode, no tools, one turn, structured JSON output.
  It runs in an empty temporary directory so your project's CLAUDE.md and hooks do not apply to
  it. Its cost is recorded separately as optimizer cost and capped by
  `budget.max_optimizer_cost_usd`.
- **manual**: writes the context and a copy of the current bundle to a proposal directory, waits
  for you to edit `candidate/` and press Enter. You do the thinking, Harness Lab runs window, gate
  and rollback.
- **fake**: for tests and the demo; marks the window's tasks as solved in `fake.yaml`.

## Leak controls

The optimizer never sees injected hidden files. Their file names and test identifiers are
replaced by `[hidden-test]`, their source lines (which unittest and pytest tracebacks quote) by
`[hidden-test-line]`, text is scrubbed before it is truncated, and rejection reasons are scrubbed
before they are shown again. Every candidate is linted before it runs and rejected if it deletes a
file, edits `harness.yaml`, exceeds `max_files` or the size caps, mentions a task id or hidden
test name, or contains a line copied verbatim from a hidden test. See
[What the optimizer sees](../explanations/optimizer-view.md).

## Reading the result

`grow show` and the dashboard's session page list every version with its status, the window
tasks it fixed, its gate pass rate, median `llm_calls` and cost on the gate, the optimizer's cost,
and the rationale it gave. The accepted versions form the lineage; the gate pass rate and
`llm_calls` from version 0 to the current version are the growth curve. `grow export` writes the
current bundle plus `lineage.json`, ready to use as a `harness:` in a sweep.

## Honest limits

With the bundled three-task suite the loop only proves the mechanics; the paper uses 200 train,
50 gate and 50 final tasks. `llm_calls` is exact for Claude Code and the generic JSONL protocol,
and `null` for Codex. The optimizer's own cost is reported next to the deployed cost, not hidden
in it. See [Honest limits](../explanations/limits.md).

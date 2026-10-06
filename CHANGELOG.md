# Changelog

## 0.2.0 (unreleased)

- Improvement tasks (`improve:` in a task): the repository already passes and the agent gets
  several rounds to make a measured objective better. Baseline and every round are evaluated on a
  scratch copy with the hidden tests, so hidden tests never reach the agent's worktree; the best
  checkpoint is kept (anchored against `git gc`) and worse rounds are reverted; an optional
  in-loop evaluator lets the agent measure a limited number of times per round. A failed
  checkpoint or revert stops the protocol without losing any round's data. A run passes when its
  final state passes the gate and beats the baseline. Metrics: baseline, best, final, ratio,
  score (1 for minimizing to 0), progress to target, curve, per-round history, evaluator calls;
  `improve_rounds` and
  `improve_eval_budget` variant options make rounds and budget sweep factors; a paired
  improvement-ratio interval. Bundled `demo-improve` suite and `improve-budget` sweep.
- Safety as a measured axis: every run gets risky-action findings from its trace (credential
  access, privilege, destructive, suite access, network, outside the worktree), each executed or
  blocked, a `safe` verdict, and per-variant safe and safe-pass rates. Tasks can plant canaries
  and lure URLs (`safety:`); reads, leaks and followed lures are findings, and canary tokens are
  detected before redaction but never recorded by a run (events, artifacts, verifier output,
  runner metadata, exports). The Claude runner records hook events (`--include-hook-events`). The
  bundled `harnesses/sentinel` is a rule-based pre-tool-use decider
  (`python -I -m harnesslab.safety.decide`, policy set with `--deny` and `--allow`) to A/B test;
  bundled `demo-safety` suite.
- `harnesslab suite mine`: builds a task suite from a repository's git history. Every commit that
  changes source and tests becomes a candidate (start at the parent, the commit's tests as hidden
  verifier, its source change as reference solution, its message as prompt with hidden test names
  scrubbed); kept only when the tests fail at the parent and pass at the commit. Writes
  `suite.yaml`, task files and `mining_report.json` with the reason for every skipped commit.
- Git fixture repositories are now copied with only the base commit and its ancestors (fetched by
  SHA; no later commits, branches, tags or remote), so an agent cannot read a mined task's answer
  out of the history. Existing internal clones are not reused.
- Paired statistics (`harnesslab.experiments.stats`): task-level cluster bootstrap intervals and an
  exact sign test, with verdicts only above a minimum number of paired tasks. Shown on the compare
  view and by `harnesslab experiment compare`, in sweep reports (recommendation vs runner-up and vs
  an optional `baseline` configuration), and available as grow gate rules
  (`gate.require: not_worse_ci | better_ci`).
- `harnesslab ablate run|report`: tests every component of a harness bundle against its own
  absence and the whole bundle against a minimal one; verdicts `helps`, `hurts`, `no evidence`,
  `not enough tasks`; dashboard section and `ablation_report` in exports. The fake runner gains
  `component_solves` and `component_llm_calls` for offline ablation demos.
- Harness bundles: a directory (system prompt, skills, hooks, agents, fake.yaml) any variant can
  carry with `harness:`; applied by the Claude Code (`--append-system-prompt-file`, `--plugin-dir`),
  Codex (prompt prefix), generic and fake runners; `harness_hash` recorded on variants and runs and
  folded into `config_hash`, so bundles work as sweep factors.
- `llm_calls` metric (Claude Code, generic JSONL; `null` for Codex) in metrics, aggregates, the
  compare view, exports and as a sweep objective.
- Growing Harness loop (`harnesslab grow run|resume|list|show|export`): failure window ->
  optimizer -> window check -> held-out gate -> accept or roll back; budgets, retirement, resumable
  state, per-version audit files; grow sessions and harness versions in the database and dashboard.
- Optimizer plugins: `claude-cli` (Claude Code binary, no tools, structured output), `manual`,
  `fake`; leak controls (scrubbed optimizer view, candidate lint) so hidden test sources, names,
  quoted lines and, by default, assertion details never reach an optimizer
  (`optimizer.verifier_detail: summary | full`); `harnesslab harness check`.
- Optimizers may not add or edit `hooks.json` unless `optimizer.allow_hooks: true`: hook commands
  run on the host outside the agent's tool allowlist. `harness check` warns about bundles with hooks.
- The `claude-cli` optimizer charges every attempt, including failed ones, to
  `max_optimizer_cost_usd`, and removes its temporary working directory.
- Failure cases shown to the optimizer always come from a failing run, also with `repetitions > 1`;
  `split.fractions` no longer rejects valid fractions because of rounding.
- Bundled `harnesses/baseline` and `grow/demo-fake.yaml`, `grow/claude-grow.yaml` templates
  (copied by `harnesslab init`).
- Documentation site (`docs/`, GitBook-compatible, built with MkDocs Material and deployed to
  GitHub Pages), a generated CLI reference and docs consistency tests.

## 0.1.0 (2026-09-22)

First release.

- Experiment runner: same task, identical base commit, one isolated git worktree per run, independent verifier
  (exit code + optional partial score), normalized traces, metrics, aggregates, JSON export.
- Harness adapters: OpenAI Codex CLI (`codex exec --json`), Claude Code CLI (`claude -p --output-format stream-json`),
  deterministic fake runner, generic command runner; plugin API (`harnesslab.api`, `plugins:` lists, entry points).
- Configuration sweeps (`harnesslab sweep`): model × reasoning effort × toolset × compaction × action policy grids
  with budgets, cheapest verified configuration per workload, factor effects, holdout tasks.
- Local dashboard (FastAPI + HTMX): experiments, task × variant matrix, run detail with timeline/diff/verifier output,
  compare view, sweep recommendations.
- Bundled demo suite (`harnesslab run demo`), `harnesslab init` scaffolding, `harnesslab doctor`.

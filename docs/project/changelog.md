# Changelog

The source of truth is [`CHANGELOG.md`](https://github.com/bilgin-kocak/harness-lab/blob/main/CHANGELOG.md)
in the repository; this page mirrors it.

## 0.2.0 (unreleased)

- Harness bundles: a directory (system prompt, skills, hooks, agents, fake.yaml) any variant can
  carry with `harness:`; applied by the Claude Code (`--append-system-prompt-file`, `--plugin-dir`),
  Codex (prompt prefix), generic and fake runners; `harness_hash` recorded on variants and runs and
  folded into `config_hash`, so bundles work as sweep factors.
- `llm_calls` metric (Claude Code, generic JSONL; `null` for Codex) in metrics, aggregates, the
  compare view, exports and as a sweep objective.
- Growing Harness loop (`harnesslab grow run|resume|list|show|export`): failure window →
  optimizer → window check → held-out gate → accept or roll back; budgets, retirement, resumable
  state, per-version audit files; grow sessions and harness versions in the database and dashboard.
- Optimizer plugins: `claude-cli` (Claude Code binary, no tools, structured output), `manual`,
  `fake`; leak controls (scrubbed optimizer view, candidate lint) so hidden tests never reach an
  optimizer; `harnesslab harness check`.
- Bundled `harnesses/baseline` and `grow/demo-fake.yaml`, `grow/claude-grow.yaml` templates
  (copied by `harnesslab init`).
- Documentation site (this site), generated CLI reference, docs consistency tests.

## 0.1.0 (2026-09-22)

First release.

- Experiment runner: same task, identical base commit, one isolated git worktree per run,
  independent verifier (exit code + optional partial score), normalized traces, metrics,
  aggregates, JSON export.
- Harness adapters: OpenAI Codex CLI (`codex exec --json`), Claude Code CLI
  (`claude -p --output-format stream-json`), deterministic fake runner, generic command runner;
  plugin API (`harnesslab.api`, `plugins:` lists, entry points).
- Configuration sweeps (`harnesslab sweep`): model × reasoning effort × toolset × compaction ×
  action policy grids with budgets, cheapest verified configuration per workload, factor effects,
  holdout tasks.
- Local dashboard (FastAPI + HTMX): experiments, task × variant matrix, run detail with
  timeline/diff/verifier output, compare view, sweep recommendations.
- Bundled demo suite (`harnesslab run demo`), `harnesslab init` scaffolding, `harnesslab doctor`.

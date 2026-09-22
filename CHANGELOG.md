# Changelog

## 0.1.0 (unreleased)

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

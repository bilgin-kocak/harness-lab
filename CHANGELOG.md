# Changelog

## Unreleased

- New adapters for three open-source harnesses that take many model providers: pi
  (`runner: pi`), OpenCode 1.x and 2.x (`runner: opencode`) and Hermes Agent (`runner: hermes`).
  Each records a full trace (messages, tool and shell calls, file changes, tokens, LLM calls) and
  the cost the harness reports (pi and OpenCode in their stream, Hermes Agent in its session
  store). Every run gets a configuration directory of its own, removed afterwards, receives
  provider API keys only, and starts with the harness's guard rails set: an injected
  deny-by-default shell allowlist for OpenCode, manual approvals that refuse dangerous commands
  for Hermes Agent; pi, which has no permission system, runs only with
  `allow_dangerous_permissions: true`. A bundle's prompt and skills go where each harness takes
  them. A run the harness refused, or that stopped before any model call, is unavailable rather
  than a failed attempt. The adapters were built from each CLI's source and are tested with
  constructed streams: not yet verified on a real run. See the Run pi, Run OpenCode and Run Hermes
  Agent guides.
- `harnesslab doctor` checks all five harness CLIs, in parallel.
- A pricing table matches a provider-prefixed model id (`openrouter/anthropic/claude-x`) by its
  last segment, and the safety rules know the tool names of pi, OpenCode and Hermes Agent.
- Website: the documentation site's front page is now a landing page (what Harness Lab does, how
  it works, when to use it, how to cite it), and a new Tour page shows every dashboard view and
  real CLI output from the demos. The former front page moved to Start here → Overview, where
  GitBook now opens. Also new: top navigation tabs, a favicon, link previews and `CITATION.cff`
  ("Cite this repository" on GitHub).
- Dashboard: long sweep configuration names wrap instead of overflowing their cards and tables,
  diffs are no longer double-spaced, a difference that rounds to zero no longer shows as "-0.0",
  and experiment dates stay on one line.
- `scripts/site_assets.py` redraws the site's images from the demos, offline.

## 0.3.0 (2026-10-12)

- Verdicts on a chosen metric: a paired comparison can judge pass rate (the default), score or
  improvement ratio, with the sign test and the minimum task count following that metric, and
  every comparison now shows a score interval. Choose it with `experiment compare --metric`,
  `ablate run --metric`, `verdict_metric` in a sweep, `gate.metric` in a grow spec (pass rate
  or score), or `?metric=` on the dashboard's compare view. For improvement tasks use `score`: it
  counts a broken final state as 0 and minimizing to 0 as 1. Differences within float noise count
  as ties. The verdict line now names its metric ("better on pass rate over ...").
- Per-task selection: sweep reports, `experiment show`, the dashboard's experiment page and
  exports say whether choosing the best variant for each task would beat the best single
  variant: the in-sample gap and a held-out gain from splitting repetitions, which is the number
  to trust. Variants without valid runs are left out by name.
- Improvement tasks can resume one agent session across rounds (`improve_session: resume`,
  through `claude --resume` and `codex exec resume`) instead of starting a fresh session each
  round; later rounds get a short delta prompt that says when files were reverted, a failed
  resume falls back to a fresh session, and a resumed Claude Code session's running cost totals
  are split per round so nothing is counted twice. New
  `fake-improver-resumed` and `claude-resumed` demo variants and an `improve-session` sweep; the
  bundled improvement sweeps judge on the improvement score.
- Several attempts: with `--repetitions`, `harnesslab run`, `experiment show`, the dashboard and
  exports report pass@k (one of k attempts passes) and best-of-k (the attempt picked by the task's
  visible check passes the hidden tests), with a bootstrap interval for best-of-k over a single
  attempt. Tasks name the visible check in `verification.visible_command`; it runs on a scratch
  copy of the worktree before the hidden files are injected, fails for an attempt that edited
  protected paths, and is recorded per run as `visible_pass`. The fake runner gains `behaviors`
  (one per repetition) and the demo a `fake-flaky` variant.
- CLI flag check: before a run, the Claude Code and Codex runners compare every flag they pass
  with the installed CLI's `--help` (colour codes removed); an incompatible CLI makes the run
  unavailable (not verified, not counted) with the flag and version named, and `harnesslab doctor`
  shows it as `incompatible`. A variant's own `extra_args` are left to the CLI, `flag_check: false`
  turns the check off, and a run the CLI refuses before printing anything is reported the same
  way. A harness that turns out to be unavailable is no longer verified as a failed attempt.
- Find-everything tasks: `verification.answer_key` grades the findings an agent writes (a JSON
  list or CSV) against a hidden key with three F1 scores taken from ATLAS: discovery (entities
  found), item (entities and attributes) and row (whole rows right). The chosen one becomes the
  score, and the run passes when it reaches `pass_threshold`. New `demo-discovery` suite with
  aliased calls and decoys.
- Recovery tasks: a task names its fault-free twin with `fault_of`, and `harnesslab run`,
  `experiment show`, the dashboard and exports report success with and without the fault and
  recovery given normal success, repetition by repetition, after UndoBench. New `demo-recovery`
  suite: three invoices paid through a payments stub that drops one acknowledgement, graded on
  exactly-once payment. `reference_solution.command` lets a reference solution be an action that
  the fake runner and `suite check` run after the overlay.
- Task metrics: the numbers a task's checks report (score command `metrics`, answer key F1s) are
  recorded per run as `task_metrics` and summarised per variant by `experiment show`, the
  dashboard and exports.
- Improvement tasks log every objective evaluation (in-loop calls and round evaluations) with the
  best verified value after it, can cap the total with `improve.max_evaluations` (variant option
  `improve_max_evaluations`) so harnesses are compared on equal budgets, and report an anytime
  score (0.7 × mean best-so-far score + 0.3 × final score, after AgenticBBO-Bench) that verdicts
  can use (`--metric anytime`, `verdict_metric: anytime`).
- Fixed: the Codex runner no longer passes `--full-auto`, which Codex CLI 0.153 rejects, so real
  Codex runs failed; `--sandbox workspace-write` selects the same sandbox.

### Fixes and hardening

- Ask before paid runs: `run`, `sweep run`, `grow run`, `grow resume` and `ablate run` say how many
  runs of which harnesses (and which grow optimizer) may use paid API usage, and under what cost
  cap, before anything starts; in a terminal they ask (default no), `--yes` skips the question,
  scripts are told and go ahead. `sweep list` shows each sweep's runners, and the bundled
  `improve-budget` and `improve-session` sweeps get a $10 cost cap.
- Fixed: the dashboard's run page returned HTTP 500 for a run still in progress or left by a
  killed process, and for every run recorded by 0.1.0 after an upgrade.
- Fixed: every run of an experiment now starts from the snapshot the experiment took of the task's
  repository; editing a fixture or moving a source repository's HEAD mid-experiment no longer
  changes later runs.
- Fixed: interrupting an experiment marked other experiments' runs as interrupted. Experiments
  record their process, and one left "running" by a killed process is marked interrupted when a
  command or the dashboard next opens the database on that machine.
- Fixed: schema upgrades also create indexes added since a database was made, and two commands
  upgrading the same old database at once no longer fail; the paths hidden-file injection
  replaced are recorded; the recorded Harness Lab commit no longer comes from whatever repository
  contains the virtualenv.
- Raw agent output is reachable: the sanitized stream, stderr, Codex's last message, the appended
  system prompt and every improvement round's files are registered artifacts (dashboard, exports).
  Runners redact stderr also when a run is cancelled; prompts are redacted before they are
  written; Codex reasoning records keep only their id and type.
- A flag value the CLI refuses at startup makes the run unavailable, with the values the CLI
  accepts, instead of a failed attempt; the `claude-cli` optimizer reports a CLI that stops before
  answering instead of retrying.
- Agent processes receive the provider variables Claude Code documents for Bedrock
  (`AWS_BEARER_TOKEN_BEDROCK`, ...), Vertex AI (`VERTEX_REGION_CLAUDE_*`, ...), Microsoft Foundry
  and model pinning; verifiers still get none.
- Breaking: every section of a task file (repo, setup, verification, inject, limits,
  reference_solution) and a sweep's objective, sample and budget reject unknown keys, so a typo
  such as `protectd_paths` is an error instead of a silently dropped setting.
- Fixed: experiment files with a `tasks:` subset (the documented format) were read as suites.
- Suite `defaults:` work: variant options by runner name (`"*"` for every runner), filled in for
  every variant that runs on the suite unless it sets them, e.g. a safe tool allowlist.
- `suites/<name>[/suite.yaml]` means the bundled suite outside a lab made by `harnesslab init`,
  with a note. `init` points the copied sweep and grow templates at the copied suites (they used
  the packaged ones) and lists everything it copies.
- `grow resume` imports the session's and its suite's plugins and takes `--plugin` and `--pricing`.
- `experiment show` prints the safety, improvement and LLM-call rows; exports inline small JSON
  artifacts (safety findings, improvement rounds); the dashboard shows improvement rows for every
  improvement experiment and the safety violations; the compare view has an LLM-calls row; a
  missing best-of-k check is reported once instead of per variant.
- `metrics_version` is 2 and the export `schema_version` is 2.
- Packaging: the sdist includes the docs, scripts and MkDocs config the tests read; the build
  needs hatchling 1.27 or newer; CI checks formatting and the distributions' contents.
- Documentation brought in line with the code throughout; the README gains a guides table.

## 0.2.0 (2026-10-06)

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

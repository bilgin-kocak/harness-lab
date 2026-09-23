# Growing Harness for Harness Lab — design

Date: 2026-09-23. Status: approved in conversation; implementation follows.

## Purpose

Let Harness Lab run a *Growing Harness* loop (Li et al., "Grow the Harness, Not the
Context", arXiv 2609.26760) for coding agents. Failures from an experiment drive edits to
a harness; a held-out gate rejects edits that regress; accepted versions accumulate; the
metrics needed to judge the result (verified pass rate, LLM calls, cost) are recorded with
the same reproducibility guarantees as every other run.

Success criteria:

1. `harnesslab grow run demo-fake` runs the whole loop on the bundled demo suite with the
   fake runner and fake optimizer in seconds, with no credentials, and shows accepted and
   rejected versions.
2. `harnesslab grow run grow/claude-grow.yaml` runs the same loop against Claude Code with an
   LLM optimizer, within a budget, resumable after interruption.
3. A grown bundle can be used as a `harness:` on any variant, including as a sweep factor,
   so "grown harness × model" experiments need no extra code.
4. Hidden tests never reach the optimizer; every candidate is linted before it runs.

Non-goals (later sub-projects): bootstrap confidence intervals, hook/skill events as trace
components, growth-curve charts, a task corpus generator, editable control code for closed
CLIs (Claude Code and Codex stay closed; the growable object is the outer harness).

## 1. Harness bundle

A bundle is a plain directory, git-trackable and hand-editable:

```
harnesses/baseline/
  harness.yaml         optional: name, description
  system_prompt.md     text appended to the agent's system prompt
  skills/<name>/SKILL.md   (any files under skills/ are allowed)
  hooks.json           Claude Code hooks, plugin hooks format ({"hooks": {...}})
  agents/<name>.md
  fake.yaml            simulation only, read by the fake runner
```

*Content files* are every file except `harness.yaml`. `harness_hash` is
`sha256` over every file's relative POSIX path and bytes, sorted by path (all files,
including `harness.yaml`). An empty directory is a valid bundle (the "strategy-free
scaffold").

Validation (`harnesslab.harness.bundle`):

- allowed paths: `system_prompt.md`, `hooks.json`, `fake.yaml`, `harness.yaml`,
  anything under `skills/` or `agents/`; nothing else, no symlinks, no path escapes;
- caps: 64 KB per file, 512 KB per bundle, 64 files;
- `hooks.json` must parse as JSON with a top-level `hooks` object; `fake.yaml` and
  `harness.yaml` must parse as YAML mappings; every `skills/<name>/SKILL.md` must start with
  YAML frontmatter containing `name`.

Optimizer edit rules (enforced by `harnesslab.harness.lint`, section 4): edit or add
content files only, never delete, never touch `harness.yaml`, at most
`optimizer.max_files` files per candidate (default 6).

### Variant integration

`harness` becomes a known field of `VariantSpec` (a path relative to the spec file that
declares the variant, or absolute). At load time the path is resolved and validated.
`RunnerConfig` gains `harness_dir: Path | None` and `harness_hash: str | None`;
`config_hash` therefore includes the bundle hash, so two variants that differ only by
bundle are distinct.

The experiment service, once per variant, snapshots the bundle to
`<home>/artifacts/<exp>/harness/<hash>/` and stores on the variant row `harness_hash` and
`harness_json` (name, description, file list with sizes); every run row stores
`harness_hash`. Sweeps expand into variants, so
`factors: {harness: {baseline: {harness: ../a}, grown: {harness: ../b}}}` works unchanged.

### Application per runner

- **Claude Code.** If `system_prompt.md` is non-empty it is concatenated (in this order)
  with the variant's `append_system_prompt` and the `action_policy` text, written to
  `<artifacts>/system_prompt.txt`, and passed with `--append-system-prompt-file`. Without a
  bundle the current `--append-system-prompt` behaviour is unchanged. If `skills/`,
  `hooks.json` or `agents/` exist, the runner materializes
  `<artifacts>/plugin/` as a Claude Code plugin (`.claude-plugin/plugin.json` with
  `name: harnesslab-<hash[:12]>`, `version: 0.0.0`; `skills/`, `agents/` copied;
  `hooks.json` copied to `hooks/hooks.json`) and passes `--plugin-dir <path>`. Both flags
  are honoured under `--bare`. The `harness_launch` system event records `harness_hash`
  and which components were applied.
- **Codex.** `system_prompt.md` followed by each skill's `SKILL.md` body (under a
  `## Skill: <name>` heading) becomes a prompt prefix placed before the action-policy
  text. `hooks.json` and `agents/` are unsupported: one `system` event named
  `harness_components_ignored` lists them.
- **Generic.** New placeholder `{harness_dir}` and env var `HARNESSLAB_HARNESS_DIR`;
  `system_prompt.md` is prepended to the prompt (all `prompt_via` modes).
- **Fake.** Reads `fake.yaml`: `solve_tasks: [ids]`, `fail_tasks: [ids]`,
  `llm_calls: int`. Per-task entries override the variant's `behavior` (solve wins over
  fail if both list a task). `llm_calls` defaults per behaviour (solve 3, partial 2, fail 2,
  noop 1) so aggregates are never empty.

Errors: a missing bundle path fails spec loading; a malformed bundle fails validation
before any run starts; a runner that cannot apply a component records it in the trace.

## 2. Grow spec

`harnesslab.grow.spec.GrowSpec`, loaded like sweeps (`load_grow_target` accepts a path or
a bundled name under `harnesslab/bundled/grow/`):

```yaml
name: claude-grow
suite: demo                            # path or bundled suite name
plugins: []
base_variant: { runner: claude, model: claude-haiku-4-5, max_turns: 30 }
harness: harnesses/baseline            # initial bundle; omit for an empty bundle
split:
  train: [fix-month-boundary, add-tag-budgets]
  gate:  [consolidate-money-formatting]
  final: []                            # optional
  # or: fractions: {train: 0.6, gate: 0.2, final: 0.2}, seed: 7
window: { size: 4, min_fixed: 1, max_attempts: 5 }
repetitions: 1
parallelism: 2
max_iterations: 10
optimizer: { kind: claude-cli, model: claude-sonnet-5, max_files: 6 }
budget: { max_runs: 200, max_cost_usd: 20, max_optimizer_cost_usd: 10 }
report: { minimize: llm_calls }        # llm_calls | cost | tokens | wall_time
keep_worktrees: false
```

Validation: `split` must be either explicit lists (disjoint, all ids in the suite, `train`
and `gate` non-empty) or `fractions` summing to ≤ 1 with `seed`; fractions are resolved
by shuffling task ids with `random.Random(seed)` and slicing (train first, then gate, then
final, at least one task each for train and gate). `window.size ≥ 1`,
`1 ≤ min_fixed ≤ size`, `max_attempts ≥ 1`, `max_iterations ≥ 1`. `optimizer.kind` must
be a registered optimizer.

## 3. The loop (`harnesslab.grow.service.GrowService`)

State per session (persisted after every step in `grow_sessions.state_json`):
`iteration`, `pool` (ordered list of train task ids currently failing),
`attempts` (task id → int), `retired` (task ids), `current_version_id`,
`consecutive_optimizer_failures`, budget counters (`runs_started`, `deployed_cost_usd`,
`optimizer_cost_usd`), `phase` (`baseline_gate | baseline_train | iterating | final | done`).

Every experiment the loop launches is an ordinary `ExperimentService.run_experiment`
with a single variant (`base_variant` + `harness: <version bundle>`, id
`v<N>`), tagged with `grow_session_id` and `grow_role`.

```
v0 = initial bundle (validated), stored as harness_versions row #0, status=initial
baseline_gate:  run gate × v0          -> gate_pass_rate(v0)
baseline_train: run train × v0         -> pool = tasks whose runs did not all pass
iterating, while iteration < max_iterations and pool non-empty and budget allows:
  W = first K tasks of pool ordered by (attempts asc, original train order)
  context = OptimizerView(current, W)           (section 4)
  proposal = optimizer.propose(context)         (errors -> version invalid, see below)
  candidate = current files + proposal.files; validate + lint
     invalid  -> version status=invalid, reason; attempts[W]++; retire; continue
  version N = materialize candidate under <home>/grow/<session>/vN/
  window:  run W × vN (repetitions)   -> fixed = {t in W : every repetition passed}
     len(fixed) < min_fixed -> status=rejected, reason="window: fixed k/K < Q"; attempts[W]++; retire; continue
  gate:    run gate × vN (repetitions) -> gate_pass_rate(vN)
     < gate_pass_rate(current) -> status=rejected, reason="gate: a -> b"; attempts[W]++; retire; continue
  accept: status=accepted; current = vN; pool -= fixed; attempts[W - fixed]++; retire
final (if split.final): run final × v0 and final × current -> final comparison
```

"Passed" means `verified_pass is True`. A task whose run is `not_verified` (infra failure)
counts as not fixed and is *not* retired for that attempt (attempts unchanged). Gate pass
rate is `n_passed / n_valid` over gate runs; if `n_valid == 0` the candidate is rejected
with reason `gate: no valid runs`. "Retire" removes tasks whose attempts reached
`max_attempts` from the pool.

Budget: one `BudgetGate` (from sweeps) across the whole session for deployed runs
(`max_runs`, `max_cost_usd`); the optimizer's cost is summed separately and checked against
`max_optimizer_cost_usd` before each proposal. Exhausting any budget ends the loop with
status `completed` and a note; nothing is left half-evaluated because the check happens
between experiments.

Failure handling: optimizer exceptions or twice-invalid output → the version is recorded
`invalid` with the error, `consecutive_optimizer_failures += 1`; at 3 the session is
`failed`. `KeyboardInterrupt`/cancellation → session `interrupted`, state saved, the
running experiment's runs are marked interrupted by the existing mechanism. Any other
exception → session `failed` with the message in `notes`. `grow resume` reloads spec and
state and continues from `phase`; a version whose window or gate experiment did not finish
is discarded (its directory stays for inspection) and the iteration restarts.

## 4. Optimizer

`harnesslab.grow.optimizers.base`:

```python
class Optimizer(ABC):
    name: str
    def __init__(self, options: dict[str, Any], *, artifacts_dir: Path | None = None): ...
    async def propose(self, context: OptimizerContext) -> Proposal: ...
```

registered with `@register_optimizer`, discovered like runners (built-ins, `plugins:`,
entry-point group `harnesslab.optimizers`).

`OptimizerContext` (pydantic): `session_name`, `iteration`, `runner`, `model`,
`bundle: dict[str, str]` (content files only), `constraints` (`allowed_paths`,
`max_files`, `max_file_bytes`, `max_bundle_bytes`, `forbidden_terms` count only),
`failures: list[FailureCase]`, `previous_rejections: list[str]` (≤ 5 reasons),
`suite_description`.

`FailureCase`: `task_id`, `task_name`, `prompt`, `attempts`, `run_id`, `status`,
`outcome`, `verified_score`, `final_message` (≤ 2 000 chars), `trace_digest` (≤ 60 lines:
`#seq kind name/command exit=… duration=…` with ≤ 200-char output previews),
`diff` (≤ 20 000 chars), `verifier_stdout`, `verifier_stderr` (≤ 8 000 chars each, redacted
and scrubbed), `metrics` (`llm_calls`, `total_tokens`, `wall_time_seconds`,
`reported_cost_usd`).

`Proposal`: `files: dict[str, str]` (relative path → full new content), `rationale: str`,
`usage: UsageTotals`, `cost_usd: float | None`, `raw: Any` (persisted as
`proposal.json`).

Implementations:

- `claude-cli` (`optimizers/claude_cli.py`): builds a system prompt (role, the edit rules,
  the bundle format, "never encode task ids, hidden test names or expected answers") and a
  user message containing the context as JSON; runs
  `claude -p --output-format json --json-schema <schema> --model <model> --max-turns 1
  --no-session-persistence --strict-mcp-config` plus a no-tools configuration verified
  against the installed CLI at implementation time (`--tools ""` or a full
  `--disallowedTools` list) with
  `build_child_env(include_auth=True)`; parses the `structured_output` of the result
  record (falls back to the last JSON object in `result`); retries once on parse failure;
  reads usage and `total_cost_usd` from the result record. Options: `model`,
  `executable`, `max_files`, `extra_args`, `timeout_seconds` (default 600).
- `manual` (`optimizers/manual.py`): writes `context.json`, `README.txt` with
  instructions, and a `candidate/` copy of the current bundle under
  `<home>/grow/<session>/v<N>-proposal/`; prints the path; waits for Enter on stdin
  (fails fast with a clear error if stdin is not a TTY); reads back `candidate/` as the
  proposal, `rationale` from `RATIONALE.md` if present.
- `fake` (`optimizers/fake.py`): test/demo. Adds each failure's `task_id` to
  `fake.yaml.solve_tasks`; option `fix_none: true` proposes an unchanged bundle;
  option `regress_gate_task: <id>` also appends that id to `fail_tasks`; option
  `invalid: true` proposes a forbidden path (`../x`). Sets `llm_calls` in `fake.yaml`
  to `options.get("llm_calls", 2)`.

### Leak controls (`harnesslab.harness.lint`)

Inputs from the suite: task ids, injected file paths (basenames and stems, e.g.
`test_hidden_budgets`), and the hidden test sources (every injected file that is text).

- *View scrub*: verifier stdout/stderr and trace previews in `FailureCase` have every
  injected basename/stem replaced by `[hidden-test]`, after the standard redactor.
- *Candidate lint*: reject when any content file (a) contains a task id as a whole
  word, (b) contains an injected basename/stem, or (c) contains any line of ≥ 24
  characters (after stripping) that appears verbatim in a hidden test source. Also enforce
  allowed paths, no deletions, caps, parseable `hooks.json`/`fake.yaml`, skill
  frontmatter. `fake.yaml` is exempt from (a) only (real runners ignore it; the fake
  optimizer needs task ids there).
- `harnesslab harness check <bundle> --suite <suite>` runs the same lint standalone.

## 5. Storage and on-disk layout

New tables (SQLAlchemy, created by `create_all`; new nullable columns added through the
existing `_add_missing_columns`):

`grow_sessions`: `id`, `name`, `suite_name`, `suite_path`, `spec_json`, `status`
(`running|completed|interrupted|failed`), `phase`, `state_json`, `initial_version_id`,
`current_version_id`, `iterations`, `created_at`, `finished_at`, `harnesslab_version`,
`harnesslab_commit`, `notes`.

`harness_versions`: `id`, `session_id` (FK, cascade), `number`, `parent_id`,
`harness_hash`, `bundle_path` (relative to home), `status`
(`initial|candidate|invalid|rejected|accepted`), `reason`, `window_task_keys_json`,
`window_fixed_json`, `window_experiment_id`, `gate_experiment_id`, `gate_pass_rate`,
`gate_n_valid`, `gate_n_passed`, `gate_llm_calls_median`, `gate_cost_median`,
`optimizer_kind`, `optimizer_model`, `optimizer_usage_json`, `optimizer_cost_usd`,
`rationale`, `created_at`, `finished_at`.

Existing tables: `experiments` + `grow_session_id`, `grow_role`
(`baseline_gate|baseline_train|window|gate|final`); `variants` + `harness_hash`,
`harness_json`; `runs` + `harness_hash`, `llm_calls`.

Disk: `<home>/grow/<session>/v<N>/` (bundle), `v<N>/proposal.json`, `v<N>/context.json`,
`v<N>-proposal/` (manual optimizer only). `grow export` copies the current accepted
bundle and writes `lineage.json` (all versions with their metrics).

## 6. `llm_calls` metric

`RunnerResult.llm_calls: int | None`, `RunMetrics.llm_calls`, `RunRow.llm_calls`,
`RunSample.llm_calls`, `VariantAggregate.llm_calls: Stat`, a compare-view delta row,
inclusion in the export, and `SweepObjective.minimize`/`tie_breaker` accept `llm_calls`
(`ConfigResult.median_llm_calls`; objective kind `llm_calls`). Sources: Claude parser
counts distinct assistant message ids; generic runner counts `usage` events (None if the
protocol is `text`); fake runner from `fake.yaml` or per-behaviour default; Codex None.

## 7. CLI, dashboard, templates

CLI: `harnesslab grow run <spec> [--dry-run] [--max-iterations N] [--name] [--plugin]
[--pricing] [--keep-worktrees]`; `grow resume <session>`; `grow list`;
`grow show <session>`; `grow export <session> <dir>`; `harnesslab harness check <bundle>
[--suite <suite>]`. `--dry-run` prints the resolved split, window size, the initial bundle
hash and the first optimizer view (from a synthetic failure list) without running
anything. `harnesslab init` also scaffolds `harnesses/baseline/` and `grow/`.

Dashboard: `/grow` (sessions list; nav link), `/grow/{session}` (summary cards: status,
phase, iterations, versions accepted/rejected/invalid, gate pass rate initial → current,
median `llm_calls` initial → current, deployed cost, optimizer cost; versions table with
links to window and gate experiments and collapsible rationale; final comparison table if
present), `/api/grow/{session}.json`. Experiment page: harness column in the variants
table; run page: `harness_hash` in the reproducibility record; experiments list: a role
chip for experiments that belong to a session.

Bundled: `bundled/harnesses/baseline/` (`system_prompt.md` with a short neutral
instruction to run the tests before finishing; empty `fake.yaml`; `harness.yaml`),
`bundled/grow/demo-fake.yaml` (fake runner, fake optimizer, split 2/1/0, window 2),
`bundled/grow/claude-grow.yaml` (Haiku deployed, Sonnet optimizer, budget capped).

## 8. Module layout

```
src/harnesslab/harness/      bundle.py (load, hash, validate, materialize helpers), lint.py
src/harnesslab/grow/         spec.py, service.py, view.py, report.py,
                             optimizers/{base,claude_cli,manual,fake}.py
src/harnesslab/runners/      claude.py, codex.py, generic.py, fake.py: apply bundles
src/harnesslab/core/         models.py (VariantSpec.harness, RunnerConfig.harness_dir/hash,
                             llm_calls), metrics.py
src/harnesslab/storage/      models.py, repository.py (grow + version methods)
src/harnesslab/experiments/  service.py (bundle snapshot, harness_hash, grow tags),
                             spec.py (harness resolution), aggregate.py, sweep.py, export.py
src/harnesslab/cli.py        grow_app, harness_app, init additions
src/harnesslab/web/          routes.py, templates/grow_list.html, grow_detail.html
src/harnesslab/bundled/      harnesses/baseline/, grow/*.yaml
tests/                       test_bundle.py, test_lint.py, test_grow.py,
                             test_optimizers.py, plus additions to existing files
```

## 9. Testing

Unit: bundle load/hash/validate (caps, escapes, symlinks, parse errors), lint (task id,
hidden stem, verbatim line, deletion, forbidden path, `fake.yaml` exemption), Claude argv
(plugin dir contents, prompt file), Codex prefix and ignored-components event, generic
placeholder/env/prepend, fake runner `fake.yaml` precedence, `llm_calls` from the recorded
Claude fixture, split resolution (explicit, fractions + seed determinism, validation
errors), GrowSpec validation, sweep `minimize: llm_calls`.

Loop (fake runner + fake optimizer, in process): accept path (pool empties), window
rejection (`fix_none`), gate regression rollback (`regress_gate_task`), invalid proposal,
retirement at `max_attempts`, `max_runs` budget stop, optimizer budget stop, resume after a
simulated interruption mid-iteration, state and disk layout, final comparison, all
experiments tagged with session and role.

Optimizers: `claude-cli` with the fake CLI replaying a canned `--output-format json`
record (structured output parsed; usage and cost captured), invalid output twice → invalid
version; `manual` fed through a stdin monkeypatch.

Storage: migration on a database created with the previous schema. Web: grow pages,
harness column, run page hash. CLI: `grow run demo-fake`, `grow show`, `grow export`,
`grow resume`, `harness check`, `init` scaffolding. Leak: after a grow session, no line
of any hidden test source appears in the database, artifacts, or grow directory.

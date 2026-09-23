# Harness Lab

**A local-first experimentation platform for evaluating AI coding-agent harnesses.**

Given the same software task and the same starting repository state, how do different agent
harnesses and configurations compare in *verified* success, token usage, tool usage, latency,
cost and behaviour? Harness Lab answers that question reproducibly:

```text
                 TASK SUITE
                     │
            identical starting repo (one git commit)
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       Harness A  Harness B  Harness C        codex · claude · fake · generic
          │          │          │
      isolated    isolated    isolated        one git worktree per run
      worktree    worktree    worktree
          │          │          │
          └──────────┼──────────┘
                     ▼
              independent verifier            hidden tests + exit code / partial score
                     │
                     ▼
            normalized experiment             provider-neutral trace + metrics + diff
                     │
                     ▼
              Harness Lab UI                   matrix · run detail · compare
```

Harness Lab is **not** an observability product. It is a scientific instrument for controlled
comparisons: the agent never decides whether it succeeded, every run starts from the same commit,
and every trace is stored in one provider-neutral schema.

## Table of contents

- [What Harness Lab is](#what-harness-lab-is)
- [Architecture](#architecture)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Running the fake demo](#running-the-fake-demo)
- [Running Codex](#running-codex)
- [Running Claude Code](#running-claude-code)
- [Test your own harness in five minutes](#test-your-own-harness-in-five-minutes)
- [Configuration sweeps: the cheapest verified configuration](#configuration-sweeps-the-cheapest-verified-configuration)
- [Growing the harness](#growing-the-harness)
- [Creating a task suite](#creating-a-task-suite)
- [Creating a harness variant](#creating-a-harness-variant)
- [Interpreting metrics](#interpreting-metrics)
- [Security warning](#security-warning)
- [Research principles](#research-principles)
- [Roadmap](#roadmap)

## What Harness Lab is

The central experimental object is

```text
TASK × MODEL × HARNESS × CONFIGURATION × ENVIRONMENT → TRACE → VERIFIER → METRICS
```

| Axis | Harness Lab object | Where it lives |
| --- | --- | --- |
| TASK | `TaskSpec` (prompt, fixture repo, verifier, limits) | `suites/<suite>/tasks/*.yaml` |
| MODEL | `VariantSpec.model` (requested) and `model_resolved` (what the harness actually used) | variant YAML, `runs` table |
| HARNESS | `VariantSpec.runner` (`codex`, `claude`, `fake`, `generic`, …) + `cli_version` | variant YAML, `runs` table |
| CONFIGURATION | every other key of a variant (`max_turns`, `sandbox`, `context_policy`, `tool_policy`, …) hashed as `config_hash` | variant YAML, `variants`/`runs` tables |
| ENVIRONMENT | `EnvironmentSpec` (sandbox kind, OS, Python, git) hashed as `environment_hash` | `experiments`/`runs` tables |
| TRACE | normalized `Event` list (`assistant_message`, `tool_started`, `command_finished`, `usage`, …) | `events` table + sanitized stream artifact |
| VERIFIER | `VerifierResult` (exit code, optional partial score, protected-path violations) | `verifier_results` table |
| METRICS | `RunMetrics` (verified pass/score, wall time, tokens, cost, tool calls, files changed, …) | `runs` table (`metrics_json` + columns) |

A **run** is one cell of that product. An **experiment** is a set of runs (tasks × variants ×
repetitions) with per-variant aggregates and pairwise comparisons.

## Architecture

```text
src/harnesslab/
  cli.py                 Typer CLI: doctor · suite list/check · run · serve · experiment list/show/export
  config.py              Settings (HARNESSLAB_HOME, default ./.harnesslab)
  core/
    models.py            TaskSpec, SuiteSpec, VariantSpec, ExperimentSpec, RunnerConfig, RunnerResult,
                         EnvironmentSpec, DiffSummary, VerifierResult, RunMetrics
    events.py            EventKind, Event, EventEmitter (sequence numbers, timestamps, redaction, batching)
    metrics.py           trace + verdict + diff → RunMetrics
    pricing.py           optional pricing.yaml → estimated_cost_usd
    ids.py               time-sortable ids, canonical hashing
  runners/
    base.py              HarnessRunner ABC + registry (runners never touch the DB or UI)
    fake.py              deterministic simulated agent (tests, demo, verifier sanity checks)
    generic.py           wrap any CLI; optional JSONL event protocol
    codex.py             OpenAI Codex CLI adapter (`codex exec --json`)
    claude.py            Claude Code adapter (`claude -p --output-format stream-json`)
  execution/
    process.py           async subprocess streaming, process-group kill on timeout, allowlisted env
    fixture.py           deterministic fixture snapshots (content-hash keyed, fixed author/date)
    worktree.py          git worktree lifecycle + diff capture against the recorded base commit
    sandbox.py           ExecutionSandbox ABC · LocalWorktreeSandbox · DockerSandbox (stub)
  trace/
    redaction.py         secret redaction (safety net, not DLP)
    normalize.py         shared normalization helpers, orphaned tool-call closure
    codex_parser.py      Codex JSONL → events (reasoning text discarded)
    claude_parser.py     Claude stream-json → events (thinking text discarded)
  verification/
    command.py           exit-code verifier, hidden-file injection, protected paths
    score.py             optional partial-score JSON verifier
  storage/
    models.py            SQLAlchemy tables: experiments, variants, tasks, runs, events,
                         verifier_results, artifacts
    repository.py        persistence API
  experiments/
    spec.py              YAML loading, built-in variants
    service.py           orchestration: worktree → agent → diff → verifier → metrics → persist
    aggregate.py         per-variant aggregates, task×variant matrix, pairwise comparison
    export.py            JSON export
  harness/
    bundle.py            harness bundles (system prompt, skills, hooks, agents, fake.yaml) + hashing
    lint.py              leak lint: task ids, hidden test names and hidden source lines never reach an optimizer
  grow/
    spec.py              GrowSpec: split, window (K, Q, R_max), optimizer, budget
    service.py           the Growing Harness loop: baseline -> window -> optimizer -> gate -> accept/rollback
    view.py              what the optimizer may see (scrubbed traces, diffs, verifier output)
    report.py            version lineage + final holdout comparison
    optimizers/          Optimizer ABC + registry; claude-cli, manual, fake
  web/                   FastAPI + Jinja2 + HTMX dashboard (experiments, runs, compare, grow sessions)
suites/demo/             the demo benchmark (fixture repo, 3 tasks, hidden tests, reference solutions)
tests/                   unit, parser (recorded JSONL fixtures), end-to-end, CLI, web, opt-in integration
```

### Run pipeline

For every task × variant × repetition cell the `ExperimentService`:

1. inserts a `running` run row (progress is visible in the dashboard while an experiment runs);
2. snapshots the fixture repository once per experiment and creates a **detached git worktree**
   under `<home>/worktrees/<experiment-id>/<run-id>/` from an internal clone, so the source
   fixture is never modified (not even its `.git`; the clone has no `origin` remote);
3. runs the task's `setup.commands` (they must leave the worktree clean);
4. launches the harness with the worktree as its working directory and an **allowlisted
   environment** (provider credentials are forwarded to the agent only, never to the verifier);
5. captures `git status`, `git diff --stat` and `git diff` **against the recorded base commit**
   (correct even if the agent committed or reset) and stores them as artifacts;
6. runs the **independent verifier** in the same worktree: protected paths → inject hidden files
   → verification command (exit code) → optional partial-score command;
7. computes metrics, persists events, verdict and artifacts;
8. removes the worktree (unless `--keep-worktrees`).

### Data layout

Everything lives under one directory (default `./.harnesslab`, override with `HARNESSLAB_HOME`):

```text
.harnesslab/
  harnesslab.db        SQLite (WAL)
  fixtures/<hash>/     materialized fixture repositories (plain directories → deterministic commit)
  repos/<hash>/        internal clones of git fixture repositories
  worktrees/<exp>/<run>/
  artifacts/<exp>/<run>/   prompt.txt · agent.diff · diff_stat.txt · git_status.txt
                           verifier_stdout.txt · verifier_stderr.txt
                           agent_stream.sanitized.jsonl · agent.stderr.log   (real harnesses)
```

## Installation

Requirements: Python 3.12+, git 2.20+, Linux or macOS (Windows: use WSL).

```bash
pip install harnesslab            # or: pipx install harnesslab  /  uv tool install harnesslab
harnesslab doctor
```

`doctor` reports Python, git, the Codex and Claude Code CLIs (missing CLIs are a warning, not an
error), `uv` and the database. Nothing else is needed for the demo: the fake runner works without any
API keys.

Developing Harness Lab itself:

```bash
git clone https://github.com/bilgin-kocak/harness-lab
cd harness-lab
uv sync                           # or: pip install -e '.[dev]'
uv run pytest
uv run harnesslab doctor
```

## Quickstart

```bash
harnesslab doctor
harnesslab run demo --variants fake-reference,fake-noop      # bundled demo suite, no credentials needed
harnesslab serve                                             # open http://localhost:8000
```

`harnesslab run demo` runs the three bundled tasks against two fake agents (one applies a known solution,
one changes nothing but claims success) so you can see the verifier, the matrix and the run pages
before spending a single token. Then:

```bash
harnesslab init my-lab && cd my-lab           # copies the demo suite + sweep templates so you can edit them
harnesslab suite check suites/demo/suite.yaml # verifiers fail on the untouched repo, pass with the reference solution
harnesslab run suites/demo/suite.yaml --variants claude-default      # real harness (needs the claude CLI)
harnesslab sweep run sweeps/demo-fake.yaml    # cheapest verified configuration, fake runner, seconds
harnesslab experiment list
harnesslab experiment show <experiment-id>
harnesslab experiment export <experiment-id> > experiment.json
python -m harnesslab --help
```

All state (SQLite database, worktrees, artifacts) lives in `./.harnesslab` (override with
`HARNESSLAB_HOME` or `--home`).

## Running the fake demo

The bundled demo suite (`harnesslab run demo`, or `suites/demo` after `harnesslab init`) targets
`ledgerlite`, a tiny standard-library Python package, with three tasks:

| Task | Kind | Hidden verifier checks |
| --- | --- | --- |
| `fix-month-boundary` | bug fix | month-end entries appear in reports; inclusive date ranges; ordering and validation unchanged |
| `add-tag-budgets` | feature across two modules | budget maths on unseen data (income ignored, multi-tag entries, exact limits), exact report formatting; partial score = fraction of hidden tests |
| `consolidate-money-formatting` | refactor with invariant | byte-identical golden output *and* a single formatting implementation (`report.py` no longer defines its own) |

Hidden tests live in `tasks/<task>/verify/` next to each task and are copied into the worktree only
when the verifier runs; visible tests are `protected_paths`, so an agent that edits them fails.

The fake runner (`runner: fake`) needs no credentials. `fake-reference` applies each task's
reference solution, `fake-noop` changes nothing but *claims* success (the verifier correctly fails
it), `fake-partial` solves one task. Other behaviours: `partial`, `fail` (a confident wrong
edit), `crash`, `timeout`. The whole dashboard can be explored with fake data:

```bash
harnesslab run demo --variants fake-reference,fake-noop,fake-partial --parallelism 3
harnesslab serve
```

## Running Codex

Requires the [OpenAI Codex CLI](https://github.com/openai/codex) on `PATH` and its credentials
(`OPENAI_API_KEY` or a `codex login`; `CODEX_HOME` is forwarded).

```bash
harnesslab doctor                      # shows "codex cli ok <version>"
harnesslab run demo --variants codex-default
```

The adapter runs `codex exec --json --full-auto --sandbox workspace-write --skip-git-repo-check
--color never -C <worktree> -c sandbox_workspace_write.network_access=false -o <last-message> -`
with the prompt on stdin, parses the JSONL stream incrementally and normalizes command
executions (with exit codes and output), file changes, MCP tool calls, web searches, todo lists,
assistant messages, per-turn usage and errors. Reasoning items are counted as `reasoning_event`;
their text is discarded before anything is written. A legacy `{"id", "msg": {...}}` stream shape
is also understood. Codex does not report cost, so `reported_cost_usd` stays `null` (use
`pricing.yaml` for estimates).

Variant options: `model`, `sandbox` (`workspace-write` default, `read-only`, or the explicit
opt-in `danger-full-access`), `network_access` (default `false`), `reasoning_effort`, `profile`,
`config_overrides` (`-c key=value`, e.g. a compaction limit), `action_policy` (prepended to the
prompt), `extra_args`, `env_passthrough`, `executable`.

Codex is not installed in the environment where this MVP was built, so the adapter is verified
against recorded JSONL fixtures (`tests/fixtures/codex/`) and against a stand-in executable that
replays them through the real adapter code (`tests/test_adapters.py`). To exercise the real CLI:
`HARNESSLAB_INTEGRATION=1 uv run pytest tests/test_integration_real.py` (from a clone).

## Running Claude Code

Requires the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) on `PATH` and
either `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` in the environment or a completed
`claude auth login` (Bedrock/Vertex variables are forwarded too).

```bash
harnesslab doctor                      # shows "claude cli ok 2.x.y"
harnesslab run demo --variants claude-default
```

The adapter runs `claude -p --output-format stream-json --verbose --max-turns 30
--permission-mode acceptEdits --permission-prompts none --no-session-persistence
--strict-mcp-config --session-id <uuid> --disallowedTools WebFetch WebSearch --allowedTools …`
with the prompt on stdin. `bypassPermissions` is refused unless a variant sets
`allow_dangerous_permissions: true`; `--include-partial-messages` and
`--forward-subagent-text` are never passed. The parser normalizes text blocks, tool calls
(`Bash` → command events with stdout/stderr, file tools → `file_change`), tool results paired by
id, subagent activity (`parent_call_id`), per-message usage (deduplicated by message id), the
final `result` record (cost, totals per model, turns, permission denials) and system events
(`api_retry`, `compact_boundary`, `permission_denied`). Thinking blocks become
`reasoning_event {count: 1}`; their text and signature are dropped before persistence.

Default shell allowlist (override per variant with `allowed_tools`):
`Read, Edit, Write, MultiEdit, Glob, Grep, LS, Bash(python *), Bash(python3 *), Bash(pytest *),
Bash(ls *), Bash(cat *), Bash(git diff *), Bash(git status *), Bash(git log *)`. Commands outside
the allowlist are denied (nobody answers prompts in headless mode); the denial count is stored
per run, and a run that ends unsuccessfully after denials is marked `blocked` rather than
`failed` so configuration problems are distinguishable from task failures.

Variant options: `model`, `max_turns`, `permission_mode`, `allowed_tools`, `disallowed_tools`,
`tools`, `max_budget_usd`, `bare` (skips hooks/CLAUDE.md/plugins; requires an API key),
`setting_sources`, `append_system_prompt`, `effort` (alias `reasoning_effort`), `autocompact`
(`--autocompact auto|100k…`), `action_policy` (`batched`, `fine` or free text appended to the system
prompt), `extra_args`, `env_passthrough`, `executable`.

## Test your own harness in five minutes

Three routes, from zero code to a distributable plugin.

**1. YAML only: wrap a command.** Any CLI that takes a prompt and edits files in its working
directory can be benchmarked with the generic runner:

```yaml
# my-suite.yaml (or a variant inside any suite)
variants:
  - id: my-agent
    runner: generic
    command: "my-agent --repo {worktree} --model {model} --effort {opt_effort}"
    effort: high                # every option is available as {opt_<name>}
    prompt_via: stdin           # stdin | file ({prompt_file}) | arg ({prompt})
    output_format: text         # or jsonl (see below)
    env_passthrough: [MY_AGENT_API_KEY]
```

```bash
harnesslab run my-suite.yaml --variants my-agent
```

With `output_format: jsonl`, any stdout line shaped like
`{"kind": "tool_started", "call_id": "1", "name": "edit", "payload": {...}}` (kinds:
`assistant_message`, `tool_started`, `tool_finished`, `command_started`, `command_finished`,
`file_change`, `usage`, `error`, `reasoning_event`) becomes a normalized event and shows up in the
timeline and metrics; `usage` lines feed token totals. Everything else is captured as text.

**2. A Python module: a real adapter.** Subclass `HarnessRunner`, emit normalized events, and point
Harness Lab at the module:

```python
# my_harness.py  (anywhere on PYTHONPATH)
from harnesslab.api import EventKind, HarnessRunner, RunnerResult, RunStatus, UsageTotals, register_runner
from harnesslab.api import build_child_env, run_process

@register_runner
class MyHarness(HarnessRunner):
    name = "my-harness"

    async def run(self, task, worktree, config, emit):
        emit.emit(EventKind.COMMAND_STARTED, call_id="1", payload={"command": "my-agent"})
        proc = await run_process(["my-agent", "--prompt", task.prompt], cwd=worktree,
                                 env=build_child_env(include_auth=True), timeout=task.limits.agent_timeout_seconds)
        emit.emit(EventKind.COMMAND_FINISHED, call_id="1", duration_ms=proc.duration_ms, payload={"exit_code": proc.exit_code})
        return RunnerResult(status=RunStatus.TIMEOUT if proc.timed_out else RunStatus.COMPLETED,
                            exit_code=proc.exit_code, final_message=proc.stdout[-2000:], usage=UsageTotals())
```

```yaml
# suite.yaml / experiment.yaml / sweep.yaml
plugins: [my_harness]
variants:
  - id: mine
    runner: my-harness
```

or `harnesslab run my-suite.yaml --plugin my_harness --variants mine`. Options on the variant
arrive as `config.options`; `config.model` carries the model.

**3. A package: entry point.** Distribute the adapter and let `pip install` register it:

```toml
[project.entry-points."harnesslab.runners"]
my-harness = "my_package.harness:MyHarness"
```

In all three cases the verifier, worktree isolation, redaction, metrics, dashboard and sweeps work
unchanged. The Codex and Claude adapters (`src/harnesslab/runners/`) are the reference
implementations.

## Configuration sweeps: the cheapest verified configuration

Models are increasingly tuned for particular action styles and runtimes. A sweep tests
`model × reasoning effort × toolset × compaction × action policy` (any factors you like) on a
workload and reports the **cheapest configuration that still passes verification**:

```yaml
# sweeps/claude-config-search.yaml (bundled template; harnesslab init copies it)
name: claude-config-search
suite: demo
base_variant: { runner: claude, max_turns: 30 }
factors:
  model: [claude-haiku-4-5, claude-sonnet-5]
  reasoning_effort: [low, high]                       # --effort
  toolset:
    minimal: { allowed_tools: [Read, Edit, "Bash(python *)"] }
    full:    { allowed_tools: [Read, Edit, Write, MultiEdit, Glob, Grep, LS, "Bash(python *)", "Bash(git diff *)"] }
  compaction:
    default: {}
    tight:   { autocompact: 100k }                     # --autocompact
  action_granularity:
    batched: { action_policy: batched }                # appended system prompt
    fine:    { action_policy: fine }
repetitions: 2                 # "verified" means the pass-rate requirement holds over repetitions
workload_by: suite             # suite | task | tag  -> one recommendation per workload
holdout_tasks: [consolidate-money-formatting]   # selected on the other tasks, reported on these
sample: { max_configs: 8, seed: 7 }             # random subset of the 32-configuration grid
budget: { max_runs: 60, max_cost_usd: 15 }      # remaining runs are recorded as "skipped"
objective:
  require: { min_pass_rate: 1.0 }               # also min_valid_runs (default: every planned run)
  minimize: cost               # reported cost -> pricing.yaml estimate -> total tokens
  tie_breaker: wall_time_seconds
```

```bash
harnesslab sweep run sweeps/demo-fake.yaml           # fake runner: seconds, no credentials
harnesslab sweep run sweeps/claude-config-search.yaml --dry-run   # print the expanded grid
harnesslab sweep run sweeps/claude-config-search.yaml             # real API usage!
harnesslab sweep report <experiment-id>              # recompute from the database
```

The report (terminal, dashboard and JSON export) contains, per workload: the recommended
configuration, the runner-up, its holdout pass rate, every configuration with its eligibility
reason, the pass-rate/cost Pareto front, and **factor effects** (marginal mean pass rate and median
cost per factor level), which is the component-ablation view. A sweep is an ordinary experiment
whose variants carry `factors`, so `experiment show`, the compare view and the export work on it too.

Honest limits:

- **Action granularity** cannot be switched inside closed CLI harnesses. `action_policy` steers it
  through an appended system prompt (Claude Code) or a prompt prefix (Codex), and the *realized*
  granularity is measured per run (`tool_calls_per_turn`, `mean_command_chars`,
  `edits_per_changed_file`). Compaction and effort are real flags for Claude Code; for Codex use
  `reasoning_effort` and `config_overrides`.
- Cost is only reported by Claude Code. Give Codex or custom harnesses a `pricing.yaml` for
  estimates; otherwise sweeps rank by total tokens and say so.
- Grids explode: use `sample`, `budget`, `tasks:` and `holdout_tasks`. Smarter search (successive
  halving, Bayesian) is on the roadmap.

## Growing the harness

Harness Lab can run the loop from *Grow the Harness, Not the Context* (Li et al., 2026,
arXiv 2609.26760) for coding agents: failures drive edits to a **harness bundle**, a held-out
**gate** rejects edits that regress, accepted versions accumulate, and the growth curve is
recorded with the same reproducibility guarantees as every other run.

### Harness bundles

A bundle is a plain directory, git-trackable and hand-editable, that any variant can carry
with `harness: path/to/bundle`:

```text
harnesses/baseline/
  harness.yaml         optional: name, description
  system_prompt.md     appended to the agent's system prompt
  skills/<name>/SKILL.md
  hooks.json           Claude Code hooks (plugin format)
  agents/<name>.md
  fake.yaml            simulation only: solve_tasks, fail_tasks, llm_calls (fake runner)
```

Claude Code and Codex are closed, so the bundle is the *outer* harness. The Claude adapter
passes `system_prompt.md` with `--append-system-prompt-file` and materializes skills, hooks and
agents as a plugin loaded with `--plugin-dir`. Codex gets the system prompt and skills as a prompt
prefix (hooks and agents are recorded as ignored). The generic runner gets `{harness_dir}` and
`HARNESSLAB_HARNESS_DIR`. Every run records `harness_hash`, and the hash is part of `config_hash`,
so `factors: {harness: {baseline: {harness: ../a}, grown: {harness: ../b}}}` works as a sweep
factor with no extra code: that is the paper's "grown harness × model" table for coding agents.

### The grow loop

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

Per iteration: fill the window with up to K failing train tasks (fewest attempts first), ask the
optimizer for a candidate, validate and lint it, run the window (a task counts as fixed only if
every repetition passes; fewer than Q fixed → rejected), run the gate (pass rate below the current
version's → rejected), otherwise accept. Rollback is implicit: "current" only moves on accept, and
the pool and attempt counters are snapshotted per version. Tasks retire at `max_attempts`. Every
window and gate evaluation is an ordinary experiment (tagged with its role in the dashboard), each
version's bundle, the exact optimizer view (`context.json`) and the raw proposal live under
`.harnesslab/grow/<session>/v<N>/`, and `grow resume` continues from the saved state.

Optimizers are plugins (`harnesslab.grow.optimizers.base.Optimizer`, entry-point group
`harnesslab.optimizers`). Built in: `claude-cli` (the Claude Code binary in print mode, no tools,
one turn, structured JSON output; its cost is recorded separately as optimizer cost), `manual`
(you edit `candidate/`, Harness Lab runs window, gate and rollback) and `fake` (tests and demo).

### What the optimizer sees, and never sees

The optimizer view holds the current bundle's content files, each failing task's prompt, a
compact trace digest, the agent's diff, the verifier's output and metrics, the edit constraints,
and the reasons of recent rejections. It never contains injected hidden files: their file names
and test identifiers (`test_*` functions, `*Tests` classes) are replaced by `[hidden-test]` and
their source lines (which unittest and pytest tracebacks quote) by `[hidden-test-line]`; text is
scrubbed before it is truncated, and rejection reasons are scrubbed before they are shown again. Every candidate is linted before it runs and rejected if it deletes a file,
edits `harness.yaml`, exceeds `max_files` or the size caps, mentions a task id or hidden test name,
or contains a line copied verbatim from a hidden test.

Honest limits: with the bundled 3-task suite the loop only proves the mechanics; the paper uses
200 train, 50 gate and 50 final tasks. `llm_calls` is exact for Claude Code (distinct assistant
messages) and the generic JSONL protocol, and `null` for Codex, whose stream does not expose model
invocations. The optimizer's own cost is reported next to the deployed cost, not hidden in it.

## Creating a task suite

Run `harnesslab init my-lab` to get an editable copy of the demo suite. A suite is a YAML file
listing task files and (optionally) variants and plugin modules:

```yaml
name: my-suite
plugins: []                    # python modules registering custom runners
tasks:
  - tasks/add-health-endpoint.yaml
variants:
  - id: claude-default
    runner: claude
    max_turns: 30
```

A task:

```yaml
id: add-health-endpoint
name: Add health endpoint
version: 1

repo:
  path: ../fixture_repo      # plain directory (materialized deterministically) or a git repo
  base_ref: main             # only used for git repositories

prompt: |
  Add a health-check function according to the repository requirements.
  Do not modify the tests.

setup:
  commands: []               # run before the agent; must leave the worktree clean

verification:
  command: python -m unittest discover -s tests -v     # exit code 0 = pass
  score_command: python .harnesslab_verify/score.py    # optional partial score (see below)
  timeout_seconds: 60
  inject:                    # copied into the worktree only when the verifier runs
    - source: add-health-endpoint/verify/test_hidden.py
      dest: tests/test_hidden.py
  protected_paths:           # changes here fail the run before the verifier runs
    - tests/

limits:
  agent_timeout_seconds: 600

tags: [python, editing]

reference_solution:          # optional: used by the fake runner and `suite check`
  overlay: add-health-endpoint/solution
```

Paths are relative to the task file. A partial-score command must write JSON to the file named
by `$HARNESSLAB_SCORE_FILE` (or print it as the last JSON line on stdout):

```json
{"score": 0.8, "max_score": 1.0, "metrics": {"tests_passed": 8, "tests_total": 10}}
```

`verified_pass` always comes from the exit code of `command`; the score only refines
`verified_score`. Keep agent-specific instructions out of the verifier; the verifier is the
contract. Run `harnesslab suite check my-suite.yaml` to confirm each verifier fails on the
untouched repository and passes with the reference solution.

Plain-directory fixtures are turned into a git repository with a fixed author, date and config,
so the base commit SHA is a pure function of the fixture's content and identical on every
machine.

## Creating a harness variant

Variants can live in a suite, in an experiment file, or come from the built-ins
(`fake-reference`, `fake-noop`, `codex-default`, `claude-default`):

```yaml
name: context-ablation
suite: ./suites/demo/suite.yaml
repetitions: 3
parallelism: 2

variants:
  - id: claude-sonnet-small-context
    runner: claude
    model: claude-sonnet-5
    max_turns: 20
    context_policy: { window: small }     # recorded on the variant for later ablation analysis
    tool_policy: { shell: allowlist }
    allowed_tools: [Read, Edit, "Bash(python *)"]

  - id: my-harness
    runner: generic
    command: "my-agent --repo {worktree} --model {model}"
    prompt_via: stdin                     # stdin | file ({prompt_file}) | arg ({prompt})
    output_format: jsonl                  # lines like {"kind":"tool_started","call_id":"1","payload":{...}}
    env_passthrough: [MY_AGENT_API_KEY]
```

Any key that is not `id`/`runner`/`model`/`description`/`harness_version`/`model_provider`/
`context_policy`/`tool_policy`/`skill_version` is passed to the runner verbatim, so new harness
knobs never require schema changes. To add a new harness, subclass
`harnesslab.runners.base.HarnessRunner`, implement `async run(task, worktree, config, emit)`
(emit normalized events, return a `RunnerResult`) and decorate it with `@register_runner`; the
Codex and Claude adapters are ~200 lines each and are good templates.

## Interpreting metrics

Per run (`runs` table / run page):

| Metric | Meaning |
| --- | --- |
| `verified_pass` | exit code 0 of the verification command (`null` when the verifier could not run) |
| `verified_score` | normalized partial score if a `score_command` exists, else 1.0 / 0.0 |
| `wall_time_seconds` | whole pipeline (agent + capture + verifier), plus `agent_wall_time_seconds` and `verifier_wall_time_seconds` |
| `input_tokens` / `cached_input_tokens` / `cache_write_tokens` / `output_tokens` | normalized: `input_tokens` are *uncached* prompt tokens for every harness (Codex reports cached tokens inside its input count; Claude reports them separately; adapters map both to this shape) |
| `reported_cost_usd` | what the harness itself reported (Claude Code does, Codex CLI does not) — never invented |
| `estimated_cost_usd` + `pricing_version` | computed only from your `pricing.yaml` (see `pricing.example.yaml`) |
| `tool_calls` / `shell_commands` / `tool_calls_unfinished` / `subagent_tool_calls` | counted from normalized events |
| `llm_calls` | model invocations: distinct assistant messages (Claude Code), `usage` events (generic JSONL), `null` for Codex |
| `files_changed` / `lines_added` / `lines_deleted` | from `git diff --numstat` against the base commit |
| `agent_exit_code` / `verifier_exit_code` / `num_turns` / `permission_denials` | raw process facts |

Run **status** (`completed`, `timeout`, `crashed`, `unavailable`, `blocked`, `interrupted`) is the
infrastructure state; **outcome** (`pass`, `fail`, `not_verified`) is the verifier's verdict.
A crashed or timed-out agent is still verified (partial work may pass); an unavailable harness is
`not_verified` and excluded from pass rates.

Per variant (experiment page, `experiment show`): success rate over *valid* runs
(`n_passed / n_valid`), mean ± std verified score, median ± std wall time, median input/output
tokens, median tool calls, median files changed, median reported and estimated cost with the
number of runs that had one, infrastructure failures. With repetitions the matrix shows `k/n`
and every individual run stays listed — averages never hide runs.

The compare view puts two variants side by side (pass rate, score, tokens, time, tool calls,
cost) and classifies each task as *both passed*, *both failed*, *A only*, *B only*, *mixed*
(repetitions disagree) or *unverified*. There is deliberately no composite "winner" score:
compare configurations on the Pareto frontier of verified success, cost, latency, tokens and
variance.

## Security warning

Harness Lab **executes coding agents and agent-written code on your machine**. The MVP isolates
runs with git worktrees, not containers:

- Agents run with your user's privileges. The Codex adapter uses Codex's own `workspace-write`
  sandbox with network disabled; the Claude adapter uses `acceptEdits` plus a shell allowlist and
  denies everything else. Neither is a security boundary against a determined agent.
- Use only trusted benchmark repositories and throwaway credentials until container isolation
  (`DockerSandbox`) exists.
- Hidden tests are copied into the worktree only at verification time, but an agent that
  searches the host filesystem could still find `suites/<suite>/tasks/*/verify`. Runs whose shell
  commands mention the suite directory are flagged (`possible_suite_access` in the run metadata).
- The environment passed to agents is an allowlist (`PATH`, locale, proxy/CA settings and
  provider credentials); verifier and setup commands receive no credentials at all. Environment
  variables are never logged.
- Before persistence, command output, messages, tool output, diffs and stderr pass through a
  **heuristic redactor** (provider `sk-*` keys, GitHub tokens, bearer tokens, AWS keys, Slack and
  Google keys, `NAME=value` assignments for secret-looking names, private-key blocks and the
  literal values of secret-looking variables in Harness Lab's own environment). **This is a safety
  net, not a data-loss-prevention system**; anything it misses is stored verbatim.
- Hidden chain-of-thought is never persisted or rendered: thinking/reasoning content is dropped
  at parse time and only `reasoning_event {count}` metadata survives; the stored provider stream
  is a sanitized copy. The test-suite scans the database and every artifact for leaked markers.
- Runaway processes are killed as a process group (SIGTERM, grace period, SIGKILL) after
  `agent_timeout_seconds`; verifiers have their own timeout.

## Research principles

The design follows recent work on agent harnesses (HarnessDev, *An Empirical Study of Harness
Design for Coding Agents*, *How Do Agent Harnesses Create Value?*, multi-harness RL and
LoopArena, among others):

1. **Model and harness are independent variables.** Every run records model (requested and
   resolved), harness and CLI version, configuration hash and environment hash separately.
2. **Prefer component ablations over framework-vs-framework comparisons.** Variants carry
   `context_policy`, `tool_policy`, `skill_version` and arbitrary options so the same harness
   can be compared with one component changed.
3. **Verification is external.** The agent's "done" is recorded as a message, never as a result.
4. **Raw evidence is preserved.** Normalized events with stable ids and ordering, diffs,
   verifier output and sanitized provider streams are kept per run for replay and failure
   analysis.
5. **Pareto frontiers, not one score.** The UI shows raw metrics side by side.
6. **Reproducibility.** Task hash, base commit, Harness Lab commit, runner config, CLI version,
   timestamps and platform are stored; experiments export to JSON.

## Roadmap

Phase 2 builds on this substrate (nothing below is implemented yet):

- **Container isolation** (`DockerSandbox`): run harness CLIs inside containers with an executor
  abstraction so hidden tests and the host are unreachable.
- **Smarter configuration search**: successive halving / Bayesian search over sweep grids,
  per-runner concurrency limits, cost-aware early stopping.
- ~~Harness Autotuner~~: shipped as `harnesslab grow` (see [Growing the harness](#growing-the-harness)).
  Next: bootstrap confidence intervals, hook/skill events as trace components, a growth-curve
  chart, and a task corpus generator so sessions run on hundreds of tasks.
- **Native Windows support** (process groups and worktree cleanup are POSIX-only today; WSL works).
- **Agent causal debugger / delta replay**: replay a trace, locate the first divergence between a
  passing and a failing run, counterfactual interventions on stable event ids.
- **Failure clustering** over normalized traces and verifier output.
- **Verifier generation** and multi-verifier scoring; live event streaming to the dashboard.
- More adapters (OpenCode, OpenAI Agents SDK, LangGraph) through `HarnessRunner`.

Not in scope by design: prompt optimization, RL, LLM judges, accounts, teams, billing,
distributed workers, Kubernetes, vector databases, model routing.

## Releasing (maintainers)

```bash
uv sync && uv run pytest && uv run ruff check src tests
uv build && uv run twine check dist/*
uv publish                     # needs a PyPI token, or:
git tag v0.1.0 && git push origin v0.1.0   # the release workflow publishes via PyPI trusted publishing
```

One-time setup for the workflow: on pypi.org add a *trusted publisher* for
`bilgin-kocak/harness-lab`, workflow `release.yml`, environment `pypi`.

## License

MIT.

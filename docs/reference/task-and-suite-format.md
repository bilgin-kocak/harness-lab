# Task and suite YAML

Both files are validated strictly: unknown keys are errors, except on variants, where unknown
keys are runner options by design.

## Suite

```yaml
name: demo                       # required
description: >                   # optional
  Three verifier-backed tasks.
plugins: []                      # python modules imported before runs (custom runners/optimizers)
tasks:                           # task files, relative to this file
  - tasks/fix-month-boundary.yaml
variants: []                     # optional variant presets (see below)
defaults: {}                     # reserved for suite-wide defaults
```

`harnesslab run <suite.yaml>` runs every task against the variants you select with `--variants`
from the suite's list, the experiment's list, or the built-ins (`fake-reference`, `fake-noop`,
`codex-default`, `claude-default`). Bundled suites are addressed by name: `harnesslab run demo`.

## Task

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | slug | required | Unique within the suite; no spaces or slashes. Used as the task key everywhere. |
| `name` | string | required | Display name. |
| `version` | int | `1` | Bump when the task changes meaning. Part of the task hash. |
| `description` | string | none | Free text. |
| `repo.path` | path | required | Plain directory or git repository, relative to the task file. |
| `repo.base_ref` | string | `HEAD` | Commit-ish to use when `repo.path` is a git repository. |
| `prompt` | string | required | What the agent is told. Its hash is recorded on every run. |
| `setup.commands` | list | `[]` | Shell commands run in the worktree before the agent, without credentials. Must leave the worktree clean. |
| `setup.timeout_seconds` | int | `120` | Per command. |
| `verification.command` | string | required | Shell command; exit code 0 means pass. Runs without credentials. |
| `verification.score_command` | string | none | Optional partial score producer (see below). |
| `verification.visible_command` *(unreleased)* | string | none | A check the agent could run itself, run on a scratch copy of the worktree before the hidden files are injected (no credentials, the same timeout; its time counts as verifier time). Its result is recorded as `visible_pass`, and best-of-k uses it to pick among repeated attempts; see [Several attempts](../guides/results.md#several-attempts-passk-and-best-of-k). |
| `verification.timeout_seconds` | int | `120` | A timed-out verifier fails the run. |
| `verification.inject` | list of `{source, dest}` | `[]` | Files or directories copied into the worktree only at verification time. `dest` must stay inside the worktree. |
| `verification.protected_paths` | list | `[]` | Paths (exact, prefix or glob) an agent may not change. Any change fails the run before the verifier runs. |
| `limits.agent_timeout_seconds` | int | `600` | The harness process is killed as a process group after this; the run is still verified. |
| `tags` | list | `[]` | Free labels; sweeps can report per tag. |
| `reference_solution.overlay` | path | none | Directory copied over the worktree by the fake runner's `solve` behaviour and by `suite check`. |
| `reference_solution.partial_overlay` | path | none | Used by the fake runner's `partial` behaviour. |
| `reference_solution.improve_overlays` | list of paths | `[]` | Improvement tasks: applied by the fake runner's `solve` behaviour one per round. |
| `reference_solution.description` | string | none | Free text. |
| `improve` | mapping | none | Turns the task into an [improvement task](../guides/improvement.md); see below. |
| `safety` | mapping | none | Canaries and lures that make the task a [safety measurement](../guides/safety.md); see below. |

### Improvement tasks

| Field | Default | Meaning |
| --- | --- | --- |
| `improve.objective.command` | required | Shell command printing one number (last number on stdout, or a JSON line with `value`). Runs without credentials on a scratch copy. |
| `improve.objective.direction` | `minimize` | `minimize` or `maximize`. |
| `improve.objective.unit` | none | Shown in prompts and reports. |
| `improve.objective.target` | none | A reference value; `improve_progress` measures the way from baseline to it. |
| `improve.objective.repeats` | `1` | Median of this many measurements. |
| `improve.objective.timeout_seconds` | `120` | Per measurement. |
| `improve.objective.inject` | `[]` | `{source, dest}` files copied in only to measure, never into the agent's worktree. |
| `improve.rounds` | `3` | Harness invocations; variant option `improve_rounds` overrides it. |
| `improve.keep_best` | `true` | Revert rounds that fail the gate or do not beat the best. |
| `improve.min_improvement` | `0.0` | Relative margin the final value must beat the baseline by to pass. |
| `improve.evaluator.budget` | `0` | In-loop measurements the agent may make per round; variant option `improve_eval_budget` overrides it. |
| `improve_session` *(unreleased)* | `fresh` | A variant option, not a task field. `fresh` runs every round in a new agent session with the full prompt; `resume` continues the first round's session with a short delta prompt. `resume` needs a runner that can resume a session (`claude`, `codex`, `fake`); any other value, or another runner, fails the run at setup. See [Fresh or resumed sessions](../guides/improvement.md#fresh-or-resumed-sessions). |

### Safety traps

| Field | Default | Meaning |
| --- | --- | --- |
| `safety.canaries` | `[]` | `{path, token}`: a repository file holding a secret token (at least 8 characters: letters, digits and `._:+/=-`). Reading it is `canary_read`; the token in an action, message or the diff is `canary_leak`. Tokens are redacted from everything a run records. |
| `safety.lure_urls` | `[]` | Planted URLs; contacting one is `lure_followed`. |

### Partial score

`score_command` runs after `command` in the same worktree, without credentials, with
`HARNESSLAB_SCORE_FILE` set to a path it should write JSON to. If it writes nothing there, the
last JSON object on its stdout is used:

```json
{"score": 0.8, "max_score": 1.0, "metrics": {"tests_passed": 8, "tests_total": 10}}
```

`verified_score` becomes `score / max_score`. Without a score command it is `1.0` for a pass and
`0.0` for a fail. `verified_pass` always comes from `command`'s exit code.

## Variant

```yaml
variants:
  - id: claude-sonnet-small-context      # required, unique
    runner: claude                       # required: fake | generic | codex | claude | a plugin name
    model: claude-sonnet-5               # optional, runner-specific meaning
    description: ...                     # optional
    harness: ../harnesses/baseline       # optional: a harness bundle directory
    harness_version: "2.1"               # optional, recorded only
    model_provider: anthropic            # optional, recorded only
    context_policy: { window: small }    # optional, recorded only (for ablation analysis)
    tool_policy: { shell: allowlist }    # optional, recorded only
    skill_version: "3"                   # optional, recorded only
    max_turns: 20                        # every other key is a runner option
```

Any key not in the list above is passed to the runner verbatim as an option. The configuration
hash covers the runner, model, options, context and tool policy and the harness bundle hash, so
two variants with identical settings and bundles hash the same.

Runner options are documented per adapter: [Run Claude Code](../guides/claude-code.md),
[Run Codex](../guides/codex.md), [Test your own harness](../guides/your-own-harness.md). The fake
runner accepts `behavior` (`solve`, `partial`, `noop`, `fail`, `crash`, `timeout`), `behaviors`
*(unreleased; a list, one per repetition in turn)*, `command`,
`run_command`, `delay_ms`, `solve_tasks`, `simulate_cost_usd_per_1k_tokens`,
`simulate_token_multiplier`, `action_policy` and `llm_calls`, and for the demo suites
`improve_break_rounds`, `eval_calls` and `simulate_unsafe` (`read_canary`, `follow_lure`,
`leak_canary`, `read_ssh_key`, `destructive`; emitted into the trace, never executed).

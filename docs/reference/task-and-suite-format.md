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
| `verification.timeout_seconds` | int | `120` | A timed-out verifier fails the run. |
| `verification.inject` | list of `{source, dest}` | `[]` | Files or directories copied into the worktree only at verification time. `dest` must stay inside the worktree. |
| `verification.protected_paths` | list | `[]` | Paths (exact, prefix or glob) an agent may not change. Any change fails the run before the verifier runs. |
| `limits.agent_timeout_seconds` | int | `600` | The harness process is killed as a process group after this; the run is still verified. |
| `tags` | list | `[]` | Free labels; sweeps can report per tag. |
| `reference_solution.overlay` | path | none | Directory copied over the worktree by the fake runner's `solve` behaviour and by `suite check`. |
| `reference_solution.partial_overlay` | path | none | Used by the fake runner's `partial` behaviour. |
| `reference_solution.description` | string | none | Free text. |

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
runner accepts `behavior` (`solve`, `partial`, `noop`, `fail`, `crash`, `timeout`), `command`,
`run_command`, `delay_ms`, `solve_tasks`, `simulate_cost_usd_per_1k_tokens`,
`simulate_token_multiplier`, `action_policy` and `llm_calls`.

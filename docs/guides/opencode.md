# Run OpenCode

> **Unreleased.** On main; ships in the next release.

Requires [OpenCode](https://github.com/anomalyco/opencode) 1.x or 2.x on `PATH`
(`npm install -g opencode-ai` for 1.x, `npm install -g @opencode/cli` for 2.x), and an API key
for your model's provider in the environment: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, `OPENCODE_API_KEY` and so on. A variant must name its model as
`provider/model`, because OpenCode's stream never says which model ran.

```bash
harnesslab doctor          # shows "opencode cli ok <version>"
```

The adapter was built against OpenCode 1.19.0 and 2.0.27 from their code, with their recorded
`--help`, and is tested with constructed streams. It has not yet been checked on a real run:
please report what differs.

## What the adapter runs

```text
opencode run --format json --title harnesslab --pure -m <provider/model> [--variant <v>]      (1.x)
opencode run --format json --title harnesslab --standalone -m <provider/model>[#<v>]         (2.x)
```

with the prompt on stdin and the worktree as the working directory. The major version comes from
`opencode --version`, or from the help when the version is unreadable. `--title` saves the model
call OpenCode would make to name the session; `--standalone` keeps 2.x from starting a background
service that outlives the run; `--pure` keeps 1.x from loading external plugins.

The JSON stream gives each model call's tokens and cost (`step_finish`), finished text blocks,
finished tool calls (`bash` as shell commands; `read`, `edit`, `write`, `glob`, `grep`, `list`,
`patch` and the rest as tools, with a file change for each edit, write and patched file) and
errors. Reasoning tokens are counted; `--thinking` is never passed, so no reasoning text reaches
the stream, and inline `<think>` spans are dropped. Provider state 2.x attaches to tool calls is
not stored.

- `reported_cost_usd` is OpenCode's cost, from its model catalog; 0 for tokens used (no price for
  the model) is recorded as `null`, for a [pricing table](../reference/pricing.md) to fill in.
- `llm_calls` and `num_turns` count model calls (steps).
- A run OpenCode refused, or that stopped before any model call (a missing key, an unknown model),
  is *unavailable*, not a failed attempt. An error after a model call ends a completed run that
  the verifier still judges.

## Isolation

Every run gets its own configuration, data, state and runtime directories (`XDG_CONFIG_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`), removed afterwards; sessions are kept in
memory (`OPENCODE_DB=:memory:`), so no transcript is written; and `OPENCODE_TEST_HOME` points
OpenCode's own idea of home there, so your `~/.claude` and `~/.agents` skills and instructions
are not read either. Your OpenCode configuration, credentials (`auth.json`), sessions and plugins
stay out of the run; the download cache (`XDG_CACHE_HOME`) is shared with your own OpenCode.
Updates, sharing, snapshots and default plugins are off, and `OPENCODE_DISABLE_MODELS_FETCH=1`
keeps the model catalog that ships with the installed version.

The repository's own `opencode.json`, `.opencode/` and `AGENTS.md` are used, as OpenCode would
use them on that repository; `project_config: false` ignores them. Only provider API keys are
forwarded, never Claude Code or Codex credentials or a subscription sign-in; forward more with
`env_passthrough`.

## Default permissions

Harness Lab injects a permission policy that mirrors its
[Claude Code defaults](claude-code.md#default-shell-allowlist):

| Permission | Default |
| --- | --- |
| `read` | allowed, except `.env` files |
| `edit` (edit, write, patch), `glob`, `grep`, `list`, `lsp`, `skill`, `todowrite` | allowed |
| `bash` | `python`, `python3`, `pytest`, `ls`, `cat`, `git diff`, `git status` and `git log` (with any arguments) allowed; every other command denied |
| `webfetch`, `websearch` | denied |
| `task` (subagents) | denied: their model calls do not appear in the run's stream |
| `question`, `external_directory`, `doom_loop` | denied |

Every rule is `allow` or `deny`, never `ask`: nobody can answer a question during a run, and a
denied call fails, the model sees why and carries on. The safety report counts it as blocked.
The policy goes in through `OPENCODE_CONFIG_CONTENT` and, for 1.x, `OPENCODE_PERMISSION`, which
1.x applies over the repository's own rules. A variant's `permission` merges over the policy key
by key, so `permission: {edit: deny}` makes a read-only run. A `permission` that allows every
shell command, every action or paths outside the worktree, and `auto: true` (`--auto`), need
`allow_dangerous_permissions: true`.

## The flag check

Before a run, Harness Lab compares every flag of the command above with `opencode run --help`.
A flag the installed version no longer lists makes the run *unavailable*, with the flag and the
version in the error, and `harnesslab doctor` shows the CLI as `incompatible`. A run OpenCode still
refuses at startup is reported the same way: 2.x names the flag ("Unrecognized flag"), and 1.x
prints only its usage, which is compared with the command line to name it. A variant's own
`extra_args` are left to OpenCode. Update OpenCode or Harness Lab, change the variant's options,
or set `flag_check: false`.

## Variant options

| Option | Default | Effect |
| --- | --- | --- |
| `model` | required | `-m provider/model`, e.g. `anthropic/claude-sonnet-4-5` |
| `variant` | unset | the provider's reasoning variant, e.g. `high`: `--variant` on 1.x, `#<variant>` on 2.x. `effort` and `reasoning_effort` are aliases |
| `agent` | OpenCode's (`build`) | `--agent` |
| `permission` | `{}` | merged over [the default policy](#default-permissions), key by key |
| `auto` | `false` | `--auto`: approve every request a rule does not deny; needs `allow_dangerous_permissions` |
| `project_config` | `true` | `false` ignores the repository's `opencode.json`, `.opencode/` and `AGENTS.md` |
| `offline` | `true` | `false` lets OpenCode fetch the models.dev catalog |
| `append_system_prompt` | unset | text added to the system prompt |
| `action_policy` | unset | `batched`, `fine` or free text added to the system prompt |
| `harness` | unset | a [harness bundle](../reference/bundle-format.md) directory |
| `allow_dangerous_permissions` | `false` | needed for a loosened `permission` or `auto` |
| `extra_args` | `[]` | appended verbatim; `--format`, `--thinking`, `--auto`, `--attach`, `--server`, `--dir`, the model and session flags are refused |
| `flag_check` | `true` | compare the flags Harness Lab passes with `opencode run --help`; see [the flag check](#the-flag-check) |
| `env_passthrough` | `[]` | extra environment variables forwarded; the `OPENCODE_CONFIG*`, `OPENCODE_PERMISSION`, `OPENCODE_DB` and `OPENCODE_TEST_HOME` variables are refused |
| `executable` | `opencode` | the binary to run |

## With a harness bundle

The bundle's `system_prompt.md`, with `append_system_prompt` and the action policy, becomes the
run's global `AGENTS.md`, which OpenCode adds to its system prompt (a copy is kept as
`system_prompt.txt` with the run's artifacts). Skills become the run's global skills
(`skills/<name>/SKILL.md`, supporting files included), which OpenCode loads natively; a skill
without a `description` gets the first line of its body. `hooks.json` and `agents/` are Claude
Code formats: the run records one `harness_components_ignored` event naming them.

## Example variant

```yaml
variants:
  - id: opencode-sonnet
    runner: opencode
    model: anthropic/claude-sonnet-4-5
    variant: high
```

Not supported yet: resuming one session across improvement rounds (`improve_session: resume`).

To exercise a real OpenCode: `HARNESSLAB_INTEGRATION=1 uv run pytest
tests/test_integration_real.py -k opencode` from a clone (`HARNESSLAB_OPENCODE_MODEL` picks the
model).

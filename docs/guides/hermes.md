# Run Hermes Agent

> **Unreleased.** On main; ships in the next release.

Requires [Hermes Agent](https://github.com/NousResearch/hermes-agent) by Nous Research on `PATH`
(`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`), and an API key for your
model's provider in the environment: `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY` and so on. Setting `provider` is recommended, since Hermes model names contain
a `/` whatever the provider.

```bash
harnesslab doctor          # shows "hermes cli ok Hermes Agent v<version>"
```

The adapter was built against Hermes Agent v0.21.5 (the 2026.9.24 release) from its source, and
is tested with constructed streams and the `hermes chat --help` of that release. It has not yet
been checked on a real run: please report what differs.

## What the adapter runs

```text
hermes chat --query-file - --format stream-json --source tool --in <worktree>
       -t file,terminal [--provider <name>] [-m <model>] [--reasoning <level>]
       [--max-turns <n>]
```

with the prompt on stdin. The stream-json output gives the answer's text, tool calls and results
(`terminal` as shell commands with exit codes; `read_file`, `write_file`, `patch` and
`search_files` as tools, with a file change for each write and patch) and the session's tokens.
Tool calls without an id are paired with their results first in, first out by tool name. Inline
`<think>` spans are dropped from the text, and the streamed pieces themselves are not stored.

The stream has neither cost nor per-call usage, so before the run's Hermes home is removed the
adapter reads the session's row from Hermes's `state.db`:

- `reported_cost_usd` is Hermes's cost for the session (actual where Hermes knows it, else its
  estimate); `null` when Hermes recorded none.
- `llm_calls` and `num_turns` are its API call count; `usage_by_model` its per-model usage,
  auxiliary models included; reasoning tokens come from there too.
- A run Hermes refused (an unknown flag or value), or that stopped before any model call (missing
  credentials, a model it will not run unattended), is *unavailable*, not a failed attempt.

## Isolation and approvals

Every run gets its own `HERMES_HOME`, a fresh directory removed afterwards. It is named
`profiles/harnesslab`, the one shape Hermes takes as given; another home could be swapped for the
profile you made active with `hermes profile use`. Your `~/.hermes` (configuration, credentials,
memory, skills, sessions) is never read or written. The run's `config.yaml`:

```yaml
memory: {memory_enabled: false, user_profile_enabled: false}
auxiliary:
  title_generation: {enabled: false, model_upgrade_enabled: false}
  background_review: {enabled: false}
curator: {enabled: false}
updates: {check: false}
approvals: {mode: manual, single_query_mode: deny}
auth: {adopt_external_logins: false}
```

Memory, the user profile, session titles, the background review and the skill curator are off:
no model calls the trace cannot see, and nothing learnt in one run reaches the next. With manual
approvals and `single_query_mode: deny`, a command Hermes considers dangerous is refused with
"BLOCKED: ..." (the default `smart` mode would ask a second model first); the model sees the
refusal and carries on, and the safety report counts the action as blocked.
`HERMES_WRITE_SAFE_ROOT` keeps `write_file` and `patch` inside the worktree, and only the
`file` and `terminal` toolsets are enabled: no web, browser or code execution. `yolo: true`
(`--yolo`, every dangerous command runs) needs `allow_dangerous_permissions: true`.

Only provider API keys are forwarded, never Claude Code or Codex credentials or a subscription
sign-in; forward more with `env_passthrough`. Hermes would otherwise borrow a Claude Code or Codex
CLI sign-in it finds (`~/.claude/.credentials.json`, `~/.codex/auth.json`) and refresh it:
`adopt_external_logins: false` turns that off, and `CLAUDE_CONFIG_DIR` and `CODEX_HOME` point at
empty directories of the run. Hermes reads the repository's `AGENTS.md` as it would anywhere.

## The flag check

Before a run, Harness Lab compares every flag of the command above with `hermes chat --help`.
A flag the installed version no longer lists makes the run *unavailable*, with the flag and the
version in the error, and `harnesslab doctor` shows the CLI as `incompatible`. A run Hermes still
refuses at startup ("unrecognized arguments", "invalid choice") is reported the same way. A
variant's own `extra_args` are left to Hermes. Update Hermes or Harness Lab, change the variant's
options, or set `flag_check: false`.

## Variant options

| Option | Default | Effect |
| --- | --- | --- |
| `model` | Hermes's default | `-m`, e.g. `anthropic/claude-sonnet-4.6` |
| `provider` | Hermes's (`auto`) | `--provider`, e.g. `openrouter` or `anthropic` |
| `reasoning` | Hermes's default | `--reasoning`: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` or `ultra`. `effort` and `reasoning_effort` are aliases |
| `toolsets` | `[file, terminal]` | `-t`; an empty list leaves Hermes's own default set |
| `max_turns` | Hermes's default | `--max-turns`: tool-calling iterations |
| `yolo` | `false` | `--yolo`; needs `allow_dangerous_permissions` |
| `append_system_prompt` | unset | text added to the system prompt |
| `action_policy` | unset | `batched`, `fine` or free text added to the system prompt |
| `harness` | unset | a [harness bundle](../reference/bundle-format.md) directory |
| `allow_dangerous_permissions` | `false` | needed for `yolo` |
| `extra_args` | `[]` | appended verbatim; `--yolo`, `--accept-hooks`, `--worktree`, `--in`, `--format`, the query, session, profile and configuration flags are refused |
| `flag_check` | `true` | compare the flags Harness Lab passes with `hermes chat --help`; see [the flag check](#the-flag-check) |
| `env_passthrough` | `[]` | extra environment variables forwarded; `HERMES_HOME`, `HERMES_YOLO_MODE`, `HERMES_ACCEPT_HOOKS`, `HERMES_WRITE_SAFE_ROOT`, `HERMES_EPHEMERAL_SYSTEM_PROMPT`, `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are refused |
| `executable` | `hermes` | the binary to run |

## With a harness bundle

The bundle's `system_prompt.md` and each skill's text, with `append_system_prompt` and the action
policy, become the session's `agent.system_prompt`, which Hermes adds to its own (a copy is kept
as `system_prompt.txt` with the run's artifacts). `hooks.json` and `agents/` are Claude Code
formats: the run records one `harness_components_ignored` event naming them.

## Example variant

```yaml
variants:
  - id: hermes-sonnet
    runner: hermes
    provider: openrouter
    model: anthropic/claude-sonnet-4.6
    reasoning: medium
```

Not supported yet: resuming one session across improvement rounds (`improve_session: resume`).

To exercise a real Hermes: `HARNESSLAB_INTEGRATION=1 HARNESSLAB_INTEGRATION_RUNNERS=hermes uv run
pytest tests/test_integration_real.py` from a clone (`HARNESSLAB_HERMES_MODEL` and
`HARNESSLAB_HERMES_PROVIDER` pick the model).

# Run Codex

Requires the [OpenAI Codex CLI](https://github.com/openai/codex) on `PATH` and its credentials
(`OPENAI_API_KEY` or a completed `codex login`; `CODEX_HOME` is forwarded).

```bash
harnesslab doctor                        # shows "codex cli ok <version>"
harnesslab run demo --variants codex-default
```

## What the adapter runs

```text
codex exec --json --full-auto --sandbox workspace-write --skip-git-repo-check
      --color never -C <worktree> -c sandbox_workspace_write.network_access=false
      -o <last-message-file> -
```

with the prompt on stdin. The JSONL stream is parsed incrementally into command executions
(with exit codes and output), file changes, MCP tool calls, web searches, todo lists, assistant
messages, per-turn usage and errors. Reasoning items are counted as `reasoning_event`; their text
is discarded before anything is written. A legacy `{"id", "msg": {...}}` stream shape is also
understood.

Two honest gaps:

- Codex does not report cost, so `reported_cost_usd` stays `null`. Give it a
  [pricing table](../reference/pricing.md) for estimates.
- Codex's stream does not expose model invocations, so `llm_calls` is `null`.

## Variant options

| Option | Default | Effect |
| --- | --- | --- |
| `model` | CLI default | `-m` |
| `sandbox` | `workspace-write` | `workspace-write`, `read-only`, or the explicit opt-in `danger-full-access` |
| `full_auto` | `true` | `--full-auto` (only with `workspace-write`) |
| `network_access` | `false` | `-c sandbox_workspace_write.network_access=` |
| `reasoning_effort` | unset | `-c model_reasoning_effort=` |
| `profile` | unset | `--profile` |
| `config_overrides` | `{}` | `-c key=value` for each entry, e.g. a compaction limit |
| `action_policy` | unset | `batched`, `fine` or free text prepended to the prompt |
| `harness` | unset | a [harness bundle](../reference/bundle-format.md) directory |
| `skip_git_repo_check` | `true` | `--skip-git-repo-check` |
| `extra_args` | `[]` | appended verbatim |
| `env_passthrough` | `[]` | extra environment variables forwarded |
| `executable` | `codex` | the binary to run |

`danger-full-access` is never selected implicitly.

## With a harness bundle

Codex has no plugin mechanism, so the bundle's `system_prompt.md` and each skill's text become a
prompt prefix placed before the action policy and the task prompt. `hooks.json` and `agents/` are
unsupported; the adapter records one `harness_components_ignored` system event naming them rather
than dropping them silently.

## Example variant

```yaml
variants:
  - id: codex-high-effort
    runner: codex
    model: gpt-5-codex
    reasoning_effort: high
    config_overrides: { model_context_window: 200000 }
    network_access: false
```

The adapter is verified against recorded JSONL fixtures and a stand-in executable that replays
them through the real adapter code. To exercise the real CLI:
`HARNESSLAB_INTEGRATION=1 uv run pytest tests/test_integration_real.py` from a clone.

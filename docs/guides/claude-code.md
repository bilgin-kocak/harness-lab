# Run Claude Code

Requires the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) on `PATH` and
either `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` in the environment, or a completed
`claude auth login`. Bedrock, Vertex and Foundry variables are forwarded too.

```bash
harnesslab doctor                        # shows "claude cli ok 2.x.y"
harnesslab run demo --variants claude-default
```

## What the adapter runs

```text
claude -p --output-format stream-json --verbose --max-turns 30
       --permission-mode acceptEdits --permission-prompts none
       --no-session-persistence --strict-mcp-config --session-id <uuid>
       --disallowedTools WebFetch WebSearch --allowedTools …
```

with the task prompt on stdin. The parser normalizes text blocks, tool calls (`Bash` becomes a
command event with stdout and stderr, file tools become `file_change` events), tool results
paired by id, subagent activity (`parent_call_id`), per-message usage deduplicated by message id,
the final `result` record (cost, totals per model, turns, permission denials) and system events
(`api_retry`, `compact_boundary`, `permission_denied`). Thinking blocks become
`reasoning_event {count: 1}`; their text and signature are dropped before anything is persisted.
`llm_calls` counts distinct assistant messages, that is, API calls.

Guard rails the adapter enforces:

- `bypassPermissions` is refused unless the variant sets `allow_dangerous_permissions: true`.
- `--include-partial-messages`, `--forward-subagent-text` and `--dangerously-skip-permissions`
  are never passed, even through `extra_args`.
- The environment is an allowlist: `PATH`, locale, proxy and CA settings, provider credentials,
  plus `DISABLE_AUTOUPDATER`, `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING` and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. A Claude Code child never sees a parent session's
  `CLAUDECODE` marker.

## Default shell allowlist

Commands outside the allowlist are denied, because nobody answers prompts in headless mode. The
denial count is stored per run, and a run that ends unsuccessfully after denials is marked
`blocked` rather than `failed`, so configuration problems stay distinguishable from task failures.

```text
Read, Edit, Write, MultiEdit, Glob, Grep, LS,
Bash(python *), Bash(python3 *), Bash(pytest *), Bash(ls *), Bash(cat *),
Bash(git diff *), Bash(git status *), Bash(git log *)
```

Override it per variant with `allowed_tools`.

## Variant options

| Option | Default | Effect |
| --- | --- | --- |
| `model` | CLI default | `--model` |
| `max_turns` | `30` | `--max-turns` |
| `permission_mode` | `acceptEdits` | `default`, `acceptEdits`, `dontAsk`, `auto`, `plan`, `bypassPermissions` (opt-in) |
| `allowed_tools` | the allowlist above | `--allowedTools` (permission rule syntax, e.g. `Bash(python *)`) |
| `disallowed_tools` | `[WebFetch, WebSearch]` | `--disallowedTools` |
| `tools` | unset | `--tools`, restricts the built-in tool set |
| `max_budget_usd` | unset | `--max-budget-usd` |
| `bare` | `false` | `--bare`: skips hooks, CLAUDE.md and plugins; needs an API key |
| `setting_sources` | unset | `--setting-sources`, e.g. `project` to ignore user settings |
| `append_system_prompt` | unset | text appended to the system prompt |
| `effort` (alias `reasoning_effort`) | unset | `--effort low|medium|high|xhigh|max` |
| `autocompact` | unset | `--autocompact auto|<tokens>`, e.g. `100k` |
| `action_policy` | unset | `batched`, `fine` or free text appended to the system prompt |
| `harness` | unset | a [harness bundle](../reference/bundle-format.md) directory |
| `extra_args` | `[]` | appended verbatim (forbidden flags rejected) |
| `env_passthrough` | `[]` | extra environment variables forwarded to the CLI |
| `executable` | `claude` | the binary to run |

## With a harness bundle

When a variant carries `harness:`, the adapter writes the bundle's `system_prompt.md` (followed by
`append_system_prompt` and the action policy) to a file and passes `--append-system-prompt-file`,
and materializes `skills/`, `hooks.json` and `agents/` as a Claude Code plugin loaded with
`--plugin-dir`. Both work under `--bare`. The launch event records the bundle hash and which
components were applied. See [Grow the harness](growing.md).

## Example variant

```yaml
variants:
  - id: claude-sonnet-tight
    runner: claude
    model: claude-sonnet-5
    max_turns: 20
    effort: high
    autocompact: 100k
    allowed_tools: [Read, Edit, "Bash(python *)"]
    harness: ../harnesses/baseline
```

Real-CLI integration tests are opt-in: `HARNESSLAB_INTEGRATION=1 uv run pytest tests/test_integration_real.py`
from a clone.

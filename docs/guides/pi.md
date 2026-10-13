# Run pi

> **Unreleased.** On main; ships in the next release.

Requires [pi](https://github.com/earendil-works/pi), the minimal coding agent
(`npm install -g @earendil-works/pi-coding-agent`), on `PATH`, and an API key for your model's
provider in the environment: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` and so
on. pi has no permission system, so a pi variant must opt in with
`allow_dangerous_permissions: true` (see [Safety](#safety)).

```bash
harnesslab doctor          # shows "pi cli ok 1.1.0"
```

The adapter was built against pi 1.1.0 from pi's source and documented JSON stream, and is tested
with constructed streams. It has not yet been checked on a real run: please report what differs.

## What the adapter runs

```text
pi --mode json --no-session --no-extensions --no-skills --no-prompt-templates --no-mcp
   --no-approve [--provider <name>] [--model <id>] [--thinking <level>]
   [--append-system-prompt <file>] [--skill <dir> ...]
```

with the prompt on stdin and the worktree as the working directory. The JSON event stream gives
assistant messages, tool calls (`bash` as shell commands with exit codes; `read`, `edit`, `write`,
`grep`, `find` and `ls` as tools, with a file change for each `edit` and `write`), each model
call's tokens and pi's own cost, retries and compactions. Thinking blocks are counted as
`reasoning_event`; their text, every signature and inline `<think>` spans are dropped before
anything is written.

- `reported_cost_usd` is pi's cost, from its model catalog. When pi has no price for the model it
  reports 0 for the tokens used; that is recorded as `null`, and a
  [pricing table](../reference/pricing.md) can estimate the cost instead.
- `llm_calls` counts the model's responses plus compaction summaries; `num_turns` counts turns
  (one response and its tool calls).
- A run in which the model never answered (a missing key, an unknown model, a provider still
  overloaded after pi's retries) is *unavailable*, not a failed attempt. pi exits 0 in JSON mode
  even then, so the adapter decides from the stream.

## Isolation

Every run gets its own `PI_CODING_AGENT_DIR`, a fresh directory that is removed afterwards, also
after a timeout or an interruption. Your pi settings, credentials (`auth.json`), sessions,
extensions, skills and prompt templates are never read or written; `--no-approve` ignores the pi
files a repository brings along (`.pi/`), and `--no-mcp` connects no MCP servers.
`PI_OFFLINE=1` keeps the model catalog that ships with the installed pi, so a run does not depend
on a catalog refresh; `PI_SKIP_VERSION_CHECK=1` and `PI_TELEMETRY=0` keep it quiet.

Only provider API keys are forwarded (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`OPENROUTER_API_KEY`, `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `MISTRAL_API_KEY`, `XAI_API_KEY` and
other providers' keys), never Claude Code or Codex credentials or a subscription sign-in. Forward
more with `env_passthrough`. pi reads the repository's `AGENTS.md` or `CLAUDE.md` as it would
anywhere; `context_files: false` turns that off.

## Safety

pi runs every tool call the model makes: its shell has your user's rights, with no allowlist and
no approval step. Harness Lab therefore refuses a pi variant without
`allow_dangerous_permissions: true`; such a run is *unavailable* and says why. Run pi in a
container, a VM or a throwaway checkout, with keys you can revoke. The
[safety report](safety.md) still records every risky action it took.

## The flag check

Before a run, Harness Lab compares every flag of the command above with
`pi --help --no-extensions`, read with a temporary agent directory. A flag the installed pi no
longer lists makes the run *unavailable*, with the flag and the version in the error, and
`harnesslab doctor` shows the CLI as `incompatible`. A run pi still refuses at startup
("Unknown option") is reported the same way. A variant's own `extra_args` are left to pi. Update
pi or Harness Lab, change the variant's options, or set `flag_check: false`.

## Variant options

| Option | Default | Effect |
| --- | --- | --- |
| `model` | pi's default | `--model`: an id, `provider/id`, or a pattern pi resolves |
| `provider` | unset | `--provider`; needs `model` |
| `thinking` | pi's default | `--thinking`: `off`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`. `effort` and `reasoning_effort` are aliases. Checked by Harness Lab, because pi only warns about a level it does not know |
| `tools` | pi's (`read,bash,edit,write`) | `--tools`, e.g. `read,bash,edit,write,grep,find,ls` |
| `exclude_tools` | unset | `--exclude-tools` |
| `context_files` | `true` | `false` passes `--no-context-files`: no `AGENTS.md` or `CLAUDE.md` |
| `offline` | `true` | `false` lets pi refresh its model catalog at startup |
| `append_system_prompt` | unset | text appended to pi's system prompt |
| `action_policy` | unset | `batched`, `fine` or free text appended to the system prompt |
| `harness` | unset | a [harness bundle](../reference/bundle-format.md) directory |
| `allow_dangerous_permissions` | `false` | required: pi has no permission system |
| `extra_args` | `[]` | appended verbatim; `--mode`, `--print`, `--api-key`, `--approve` and the session flags are refused |
| `flag_check` | `true` | compare the flags Harness Lab passes with pi's `--help` before a run; see [the flag check](#the-flag-check) |
| `env_passthrough` | `[]` | extra environment variables forwarded; `PI_CODING_AGENT_DIR` is refused |
| `executable` | `pi` | the binary to run |

## With a harness bundle

The bundle's `system_prompt.md`, with `append_system_prompt` and the action policy, is appended
to pi's system prompt through `--append-system-prompt`, from a `system_prompt.txt` kept with the
run's artifacts. Skills load natively: each `skills/<name>/` directory, supporting files
included, is written next to it and passed with `--skill`. A skill whose frontmatter has no
`description` gets the first line of its body, because pi does not load a skill without one.
`hooks.json` and `agents/` have no pi counterpart: the run records one
`harness_components_ignored` event naming them.

## Example variant

```yaml
variants:
  - id: pi-sonnet
    runner: pi
    model: anthropic/claude-sonnet-4-5
    thinking: medium
    allow_dangerous_permissions: true   # pi has no permission system: sandbox the run
```

Not supported yet: resuming one session across improvement rounds (`improve_session: resume`).

To exercise a real pi: `HARNESSLAB_INTEGRATION=1 HARNESSLAB_INTEGRATION_RUNNERS=pi uv run pytest
tests/test_integration_real.py` from a clone, with `HARNESSLAB_PI_MODEL` naming the model.

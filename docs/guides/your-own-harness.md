# Test your own harness

Three routes, from zero code to a distributable plugin. In every case the verifier, worktree
isolation, redaction, metrics, dashboard, sweeps and grow sessions work unchanged.

## 1. YAML only: wrap a command

Any CLI that takes a prompt and edits files in its working directory can be benchmarked with the
`generic` runner:

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

Placeholders: `{worktree}`, `{model}`, `{prompt_file}`, `{prompt}` (only with
`prompt_via: arg`), `{task_id}`, `{harness_dir}` and `{opt_<name>}` for every other option. All
values are shell-quoted. The command runs through `/bin/sh -c` in the worktree with an
allowlisted environment plus `HARNESSLAB_HARNESS_DIR` when a bundle is attached. If the variant
carries a [harness bundle](../reference/bundle-format.md), its `system_prompt.md` is prepended to
the prompt.

### The JSONL event protocol

With `output_format: jsonl`, any stdout line shaped like

```json
{"kind": "tool_started", "call_id": "1", "name": "edit", "payload": {"path": "a.py"}}
```

becomes a normalized event and shows up in the timeline and metrics. Kinds:
`assistant_message`, `tool_started`, `tool_finished`, `command_started`, `command_finished`,
`file_change`, `usage`, `error`, `reasoning_event`. `usage` lines feed the token totals and each
one counts as one `llm_calls`. `reasoning_event` payloads are replaced by `{count: 1}`. Every
other line is captured as assistant text. See [Events](../reference/events.md).

## 2. A Python module: a real adapter

Subclass `HarnessRunner`, emit normalized events, and point Harness Lab at the module:

```python
# my_harness.py  (anywhere on PYTHONPATH)
from harnesslab.api import (
    EventKind, HarnessRunner, RunnerResult, RunStatus, UsageTotals, register_runner,
    build_child_env, run_process,
)


@register_runner
class MyHarness(HarnessRunner):
    name = "my-harness"

    async def run(self, task, worktree, config, emit):
        emit.emit(EventKind.COMMAND_STARTED, call_id="1", payload={"command": "my-agent"})
        proc = await run_process(
            ["my-agent", "--prompt", task.prompt],
            cwd=worktree,
            env=build_child_env(include_auth=True),
            timeout=task.limits.agent_timeout_seconds,
        )
        emit.emit(
            EventKind.COMMAND_FINISHED,
            call_id="1",
            duration_ms=proc.duration_ms,
            payload={"exit_code": proc.exit_code},
        )
        return RunnerResult(
            status=RunStatus.TIMEOUT if proc.timed_out else RunStatus.COMPLETED,
            exit_code=proc.exit_code,
            final_message=proc.stdout[-2000:],
            usage=UsageTotals(),
            llm_calls=None,
        )
```

```yaml
# suite.yaml / experiment.yaml / sweep.yaml / grow.yaml
plugins: [my_harness]
variants:
  - id: mine
    runner: my-harness
```

or `harnesslab run my-suite.yaml --plugin my_harness --variants mine`. Options on the variant
arrive as `config.options`; `config.model` carries the model; `config.harness_dir` and
`config.harness_hash` carry the bundle if one is attached. Implement `check_availability` to make
`harnesslab doctor` and the `unavailable` status work for your harness. The full contract is in
[Python API](../reference/python-api.md); the Codex and Claude adapters are the reference
implementations.

## 3. A package: entry point

Distribute the adapter and let `pip install` register it:

```toml
[project.entry-points."harnesslab.runners"]
my-harness = "my_package.harness:MyHarness"
```

Optimizers for [grow sessions](growing.md) register the same way through
`harnesslab.optimizers`.

## What a good adapter does

- Runs the harness with the worktree as its working directory and never writes outside it.
- Builds its environment with `build_child_env(include_auth=True, ...)` so credentials reach the
  agent but nothing else leaks, and passes `env_passthrough` for anything extra.
- Emits paired `*_started` / `*_finished` events with the same `call_id`, so the timeline shows
  durations and unfinished calls are counted.
- Never emits hidden reasoning text; emit `reasoning_event` with a count instead.
- Returns `RunStatus.TIMEOUT` when it was killed, `UNAVAILABLE` when the binary is missing,
  `BLOCKED` when it was denied permissions it needed, and lets the verifier decide the outcome.
- Reports `usage`, `usage_by_model`, `reported_cost_usd` (only if the harness reports one),
  `llm_calls`, `model_resolved` and `cli_version` when it can.

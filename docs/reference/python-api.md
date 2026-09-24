# Python API

The public extension surface is `harnesslab.api` (also importable lazily from `harnesslab`) plus
the optimizer interface in `harnesslab.grow.optimizers.base`. Everything else is internal and may
change between minor versions.

## Runners

```python
from harnesslab.api import (
    Availability, Event, EventEmitter, EventKind, HarnessRunner, RunStatus,
    RunnerConfig, RunnerResult, TaskSpec, UsageTotals, VariantSpec,
    available_runners, build_child_env, register_runner, run_process, shell_argv,
)
```

### `HarnessRunner`

```python
class HarnessRunner(ABC):
    name: str                      # the value of `runner:` in YAML
    description: str = ""

    def __init__(self, *, artifacts_dir: Path | None = None): ...
    async def run(self, task: TaskSpec, worktree: Path, config: RunnerConfig,
                  emit: EventEmitter) -> RunnerResult: ...          # required
    async def check_availability(self, config: RunnerConfig | None = None) -> Availability: ...
```

`run` executes the harness with `worktree` as its working directory and translates its output
into events through `emit`. `artifacts_dir` is a directory the runner may fill with large
auxiliary files (a sanitized stream, stderr). Runners never touch the database or the UI.
Register with `@register_runner`, list the module under `plugins:`, pass `--plugin`, or expose it
through the `harnesslab.runners` entry-point group.

### `RunnerConfig`

| Field | Meaning |
| --- | --- |
| `runner`, `model`, `variant_id` | From the variant. |
| `options` | Every unknown variant key, verbatim. `config.get("max_turns", 30)`. |
| `context_policy`, `tool_policy` | Recorded policies, if the variant set them. |
| `harness_dir`, `harness_hash` | The harness bundle, if the variant carries one. |
| `config_hash()` | Stable hash of everything but `variant_id` and `harness_dir`. |

### `RunnerResult`

| Field | Meaning |
| --- | --- |
| `status` | `RunStatus`: `COMPLETED`, `TIMEOUT`, `CRASHED`, `UNAVAILABLE`, `BLOCKED`, `INTERRUPTED`. |
| `exit_code`, `final_message`, `error` | Process facts. The final message is recorded, not trusted. |
| `usage`, `usage_by_model` | `UsageTotals` per the normalized token shape. |
| `reported_cost_usd` | Only if the harness reported one. |
| `llm_calls`, `num_turns`, `permission_denials` | Counts, when known. |
| `provider_session_id`, `model_resolved`, `cli_version` | Provenance. |
| `metadata` | Free-form dict stored with the run. |

### `EventEmitter`

```python
emit.emit(kind, *, name=None, payload=None, source=None, duration_ms=None,
          call_id=None, parent_call_id=None, raw_metadata=None, timestamp=None) -> Event
emit.run_id; emit.redactor; emit.events
```

Assigns sequence numbers and timestamps, redacts payloads, and flushes batches to storage while
the run is executing. See [Events](events.md).

### Process helpers

```python
env = build_child_env(include_auth=True, passthrough=["MY_VAR"], overrides={"X": "1"})
proc = await run_process(argv, cwd=worktree, env=env, timeout=600, stdin_text=prompt,
                         on_stdout_line=callback, stderr_path=path)
argv = shell_argv("python -m pytest")           # ["/bin/sh", "-c", ...]
```

`build_child_env` starts from an allowlist (`PATH`, `HOME`, locale, proxy and CA variables) and
adds provider credentials only with `include_auth=True`. `run_process` streams stdout line by line
without a line-length limit, writes stderr to a file or keeps a tail, kills the whole process group
on timeout (SIGTERM, grace period, SIGKILL) and returns a `ProcessResult` even when the executable
is missing (`error` is set).

## Optimizers

```python
from harnesslab.grow.optimizers.base import (
    EditConstraintsSpec, FailureCase, FailureMetrics, Optimizer, OptimizerContext,
    OptimizerError, Proposal, available_optimizers, create_optimizer, register_optimizer,
)
```

```python
@register_optimizer
class MyOptimizer(Optimizer):
    name = "my-optimizer"

    def __init__(self, options: dict[str, Any], *, artifacts_dir: Path | None = None): ...
    async def propose(self, context: OptimizerContext) -> Proposal: ...
```

`OptimizerContext` holds `session_name`, `iteration`, `runner`, `model`, `bundle` (content files,
path → text), `constraints` (allowed paths, `max_files`, byte caps), `failures` (a `FailureCase`
per window task: prompt, attempts, status, outcome, score, final message, trace digest, diff,
verifier output, metrics, all scrubbed), `previous_rejections` and `suite_description`.

`Proposal` returns `files` (path → full new content for every file the candidate should contain),
`rationale`, the optimizer's own `usage` and `cost_usd`, and `raw` (persisted for audit). An
optimizer never runs the agent and never touches the database; the grow service validates, lints,
evaluates and accepts or rejects. Register through `plugins:` in a grow spec or the
`harnesslab.optimizers` entry-point group.

## Task and variant models

`TaskSpec`, `VariantSpec`, `SuiteSpec`, `ExperimentSpec` and `SweepSpec` are pydantic models in
`harnesslab.core.models`; `GrowSpec` lives in `harnesslab.grow.spec`. `TaskSpec.resolve(path)`
resolves task-relative paths, `spec_hash()` and `prompt_hash()` give the recorded hashes.

## Programmatic runs

```python
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, VariantSpec
from harnesslab.experiments.service import ExperimentService
from harnesslab.experiments.spec import load_suite
from harnesslab.storage.database import Database

settings = Settings.from_env()
db = Database(settings.resolved_database_url); db.create_all()
suite, tasks = load_suite("suites/demo/suite.yaml")
service = ExperimentService(settings, db)
outcome = await service.run_experiment(
    ExperimentSpec(name="api", suite="suites/demo/suite.yaml"),
    suite, tasks, [VariantSpec(id="ref", runner="fake", behavior="solve")],
)
```

`ExperimentService`, `GrowService` and `Repository` are stable enough to script against but are
not yet part of the frozen public API.

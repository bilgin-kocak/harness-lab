# ADR 2026-11-22: a domain-neutral adapter contract

- **Status:** proposed, waiting for approval. Nothing below is implemented yet.
- **Sprint:** 21–27 November 2026 (Annealab general-optimization sprint).
- **Audited:** `main` at `58d8acd` (harnesslab 0.3.0 plus the unreleased pi, OpenCode and Hermes
  Agent runners). All 433 existing tests pass there (5 real-CLI integration tests skipped).
- **Source of requirements:** the sprint brief `annealab-general-optimization-sprint.md` was not
  available in this session (the attachment did not arrive). The six contract methods below are
  the ones named in the session request; the split of responsibilities follows the brief's
  summary in `annealab/PRODUCT.md` ("the core owns search, budgets, scheduling, evaluation splits,
  statistics, ablations, provenance and promotion decisions; a domain adapter owns the tasks, the
  settings that can change, the independent verifier and the domain's metrics"). Where the brief
  says otherwise, the brief wins and this ADR is updated.

## Context: how the engine works today

| Part | What it does | Domain-neutral? | Status |
| --- | --- | --- | --- |
| Run pipeline (`experiments/service.py`) | Per cell (task × variant × repetition): snapshot the fixture repo, create a git worktree, run setup, launch the harness runner, capture `git diff`, run the verifier, compute metrics, persist. Concurrency up to `parallelism`. | No: worktrees, setup commands, diffs and runners are coding concepts. | Shipped |
| Verifier (`verification/command.py`) | Protected paths, optional visible check on a scratch copy, hidden-file injection, verification command (exit code), optional partial score or answer-key grading. Runs after the agent exits, without provider credentials. | No: `Verifier.verify(task, sandbox, ctx, changes)` needs a worktree and a diff. | Shipped |
| Sweep (`experiments/sweep.py`) | Cartesian product of factor levels over a base variant, optional seeded random subsample (the baseline is never sampled out). `BudgetGate(max_runs, max_cost_usd)` is checked before each cell starts; later cells are recorded as `skipped`. Picks the cheapest configuration whose pass rate meets `min_pass_rate`; Pareto front, marginal factor effects, paired comparisons against the runner-up and the baseline, routing gap. | Partly: expansion, the budget gate and the analysis work on generic samples; variants are coding `VariantSpec`s. | Shipped |
| Sweep holdout (`holdout_tasks`) | Holdout tasks run **for every configuration, in the same experiment** as the search tasks; they are excluded from selection afterwards and the recommended configuration's holdout pass rate is reported. | Yes | Shipped, but **not a sealed holdout**: every configuration's holdout results exist before the choice, no freeze record is written, and nothing prevents re-running after looking. |
| Budget accounting | Cost counts reported cost, else the pricing-table estimate. A cell's cost is known only when it finishes, so with `parallelism > 1` the cap can be overshot by the cells already running; harnesses with neither reported nor estimated cost never trip `max_cost_usd`. | Yes | Shipped |
| Ablation (`experiments/ablation.py`) | Full bundle, minimal bundle and one leave-one-out bundle per component (system prompt, each skill, each agent, hooks), run on the same tasks; paired interval and exact sign test per component. | No: components are harness-bundle files. | Shipped |
| Grow (`grow/`) | Optimizer edits a harness bundle; a candidate is accepted only if it fixes enough failures in a window and does not regress a **gate** split (`no_regression`, `not_worse_ci` or `better_ci`). A **final** split runs only v0 and the accepted version, after the loop. Leak lint keeps hidden tests away from the optimizer. | No: bundles, optimizers and the failure view are coding concepts. | **Experimental**: no real Claude Code grow session has been run. The final split is the closest thing to a sealed holdout today, but no freeze artifact is written before it runs, and the gate split is reused for every accept/reject decision (it is a validation set, not a test set). |
| Statistics (`experiments/stats.py`) | Paired by task: repetitions averaged within a task, percentile cluster bootstrap of the mean difference (2,000 resamples, seeded), exact two-sided sign test with ties dropped, verdict only with at least `min_tasks` (5) paired tasks. | Yes: pure functions over per-task values. The module imports `VerdictMetric` from `core/models.py` and annotates `RunSample`, which ties it to the coding models. | Shipped |
| Storage (`storage/`) | One SQLite database (WAL) under `.harnesslab/`: experiments, variants, tasks, runs, events, verifier results, artifacts, grow sessions. Run rows carry coding columns (files changed, lines added, shell commands, tool calls, visible pass). Forward-only column additions. | No | Shipped |
| Dashboard (`web/`) | FastAPI, Jinja and htmx, tables only: experiment list, task × variant matrix, compare (paired), run detail with timeline and artifacts, grow sessions; JSON export. No live streaming, no charts. | No: views assume runs, diffs and traces. | Shipped |
| Runners | Claude Code and Codex (released); pi, OpenCode, Hermes Agent (on `main`, not yet verified on a real run); fake; generic CLI wrapper. | No | Mixed |

What is missing for this sprint: nothing in the engine can express a task that is not a git
repository, a verifier that is not a shell command in a worktree, or a configuration that is not
a coding-agent variant. And the only holdout the sweep offers is not sealed.

## Decision

### Two layers

- **The core: a new package `harnesslab.study`.** It owns the dev/held-out split, search within a
  budget, scheduling and concurrency, the freeze record, the one-time held-out evaluation, paired
  statistics, provenance and the stored records. It imports only the standard library, `pydantic`
  and its own modules. A test parses every module of the package and fails on any other import,
  so the core cannot import Lean, Qiskit, an LLM SDK, prompts, or the coding pipeline
  (`harnesslab.runners`, `execution`, `verification`, `harness`, `grow`, `experiments.service`,
  `core.models`).
- **Adapters: `harnesslab.domains.<name>`.** `coding`, `qiskit` (QASMBench) and `lean`
  (miniF2F). Everything domain-specific lives there: datasets, knobs, prompts, model calls,
  toolchains, verifiers, metrics. Adapters are found by name through a registry and the
  `harnesslab.domains` entry-point group, the same pattern as runners. Their heavy dependencies
  are optional extras and are imported only inside the adapter.

### The contract

```python
class DomainAdapter(Protocol):
    name: str      # registry name, e.g. "qiskit-qasmbench"
    version: str   # recorded with every attempt

    def load_tasks(self, ids: Sequence[str]) -> list[Task]: ...
    def search_space(self) -> SearchSpace: ...
    def baseline_config(self) -> Config: ...
    async def run(self, task: Task, config: Config, ctx: RunContext) -> Attempt: ...
    async def verify(self, task: Task, attempt: Attempt, ctx: VerifyContext) -> Verdict: ...
    def metrics(self, task: Task, attempt: Attempt, verdict: Verdict) -> dict[str, float | None]: ...

    # optional
    def provenance(self) -> dict[str, Any]: ...      # dataset commit, toolchain and model pins
    def audit_split(self, dev: list[Task], held_out_ids: Sequence[str]) -> list[str]: ...
    def max_attempt_cost_usd(self, config: Config) -> float | None: ...  # worst case, reserved
    async def close(self) -> None: ...
```

The types belong to the core and are plain data (`pydantic`, JSON-serializable):

| Type | Fields | Notes |
| --- | --- | --- |
| `Task` | `id`, `content_hash`, `data` (adapter-owned JSON), `tags` | The core records `content_hash` and refuses a held-out task whose hash differs from the frozen split. |
| `SearchSpace` | named knobs, each with a finite list of JSON levels | Data, not code. The core enumerates, samples and records levels; it never interprets them. |
| `Config` | `values` (knob → level) | `config_hash` = SHA-256 of canonical JSON (sorted keys). |
| `Attempt` | `task_id`, `config_hash`, `repetition`, `status` (`completed`, `timeout`, `error`, `refused`, `unavailable`), `output` (candidate artifact or a path to it), `usage` (`cost_usd`, tokens, `llm_calls`), `wall_time_s`, `notes` | Produced by the system under test. It never says whether it succeeded. |
| `Verdict` | `status` (`pass`, `fail`, `unverifiable`), `reason`, `checker` (name and version), `details`, `duration_s` | `unverifiable` (for example, too large to check) is never counted as a pass. |
| metrics | `dict[str, float | None]` | Domain numbers (two-qubit gates, depth; LLM calls, refusals). The core adds wall time, cost and the verdict. |

Rules each method must follow:

1. `load_tasks(ids)` returns exactly the tasks named. The core passes only the IDs of the current
   phase: the search never receives held-out IDs, and held-out IDs are loaded only after a freeze
   record exists.
2. `search_space()` and `baseline_config()` are pure; the baseline must be a point of the space.
3. `run()` executes the system under test once. It may call models, compilers or agents and must
   report every cost it incurs (the core's budget gate reserves cost before a cell starts and
   settles it after).
4. `verify()` is the independent check. It does not read the system's own claims as evidence.
5. `metrics()` is pure: the same attempt and verdict always give the same numbers.
6. `provenance()` holds pins only (versions, commits, hashes), never paths, timings or machine
   state: the held-out phase refuses to start if it differs from the value recorded at the freeze.
7. `max_attempt_cost_usd(config)`, when given, is a true upper bound on what one attempt with
   `config` can spend. The core reserves the lower of it and the spec's per-attempt cap, and
   passes that as the attempt's hard limit. So a cheap configuration does not hold a large
   reservation, and the limit never cuts an attempt short before its own caps.
8. Failures split in two:
   - `run()` raising an ordinary exception is a result of the system under test: a failed cell,
     charged its whole reservation.
   - `run()` raising `HarnessError`, `verify()` or `metrics()` raising, or an invalid cost is a
     broken harness. The cell is recorded with no outcome and can run again once the harness is
     fixed, and the phase stops.

### The study protocol (core)

1. **Plan.** A study spec names the adapter and its options, the fixed dev and held-out IDs (from
   a committed split file), repetitions, the search strategy (`grid`, `random` with a seed, or an
   explicit staged list), the objective (feasibility from verdicts, then metrics with directions
   and tie-breaks), the budget (attempts, USD, wall time) and the statistics settings.
2. **Search.** Baseline and candidate configurations run on dev tasks only, inside the budget.
   Every attempt, verdict and metric set is appended to `dev/attempts.jsonl`.
3. **Freeze.** The selected configuration is written to `freeze.json`: config and its hash, the
   baseline and its hash, any extra comparators, the dev results the choice rests on, the
   objective values, a UTC timestamp, the harnesslab commit, adapter provenance, and SHA-256
   hashes of the dev and held-out ID lists. The file is write-once and is committed to git before
   the held-out phase starts.
4. **Held-out.** Refuses to start without `freeze.json`. Runs the baseline, the frozen
   configuration and the declared comparators on the held-out tasks, in a seeded interleaved
   order. Every cell runs once: a cell that already has a record is never run again (a crash can
   be resumed, a finished cell cannot be redone).
5. **Report.** Paired statistics per task (bootstrap interval, exact sign test, exact McNemar for
   binary outcomes), cost, latency and failures, into `results.json`; a human writes the
   narrative report from it.

Storage for this sprint is append-only JSONL plus JSON files in the study directory, not the
SQLite database: the records are what the reports cite, and they can be committed.

Hardening added after an adversarial review of the first version (2026-11-22):

- Every step that writes holds an exclusive lock on the study directory.
- Each cell writes a reservation line before it starts, and an interrupted cell is charged in
  full.
- A torn last record is repaired before the next append.
- Each search stage fixes its configurations and cells (`plans/`) the first time it runs, so
  resuming adds and changes nothing.
- After a confirm stage, the freeze chooses among the confirmed configurations only.
- An overlapping split runs nothing.
- Before the held-out phase, the core checks that these still match the freeze, and re-ranks the
  dev records to confirm the frozen choice:
  - the split file;
  - the adapter's name, version, options and provenance;
  - the objective, statistics and held-out sections of the spec;
  - the dev records.
- Reports mark an incomplete held-out phase and never count an unrun cell as a loss.

Hardening added after a red-team review of the core (2026-11-23), one line per finding:

- A stage that runs nothing stores that decision in its plan; `search` stops at an unfinished stage.
  A plan whose stage was renamed or removed since stops `search` and the freeze.
- In any stage that contains the baseline, the baseline's cells run first.
- Refine, combine and confirm stages refuse to run until every stage before them has finished.
- A budget refusal waits only while reservations are in flight, so float residue cannot hang it.
- An attempt that cannot be stored leaves a harness-error record with its charge and reservation.
  The attempt the core writes for a run that raised is always storable, so it stays a failure.
- Combine counts only single-knob additions on top of the reference, never undoing its knobs.
- An unavailable attempt is a harness error: never verified, never a loss, and the cell runs again.
  The coding adapter reports one only when the harness never ran.
- `results.json`, the Markdown tables and `study check` say when robustness is not compared.
- Plans and the freeze hash the spec as written, so a new optional field invalidates nothing; the
  freeze also keeps the sealed sections in effect, so a default changed since is refused.
- Only the task or thread that took the study lock may enter it again; any other step in the
  process is refused, even one the holder started as a task.
- `study check` validates comparator levels; a configuration error, or a stage naming an unknown
  knob, exits 2 with a message.

### How the coding adapter maps onto the contract

| Contract | Coding adapter, using today's objects unchanged |
| --- | --- |
| `load_tasks` | `load_suite()` → `TaskSpec` per ID; `content_hash` = the task's spec hash combined with the fixture's base commit (what `task_hash` records today). |
| `search_space` | The sweep's factors (`FactorLevels`) over its `base_variant`. |
| `baseline_config` | The sweep's `baseline` levels, or a named suite variant. |
| `run` | `ExperimentService.execute_run` for one cell: worktree, setup, runner, diff capture, hidden-test verifier, metrics. Returns the stored run as the attempt; it is `unavailable` only when the harness never ran. |
| `verify` | Returns the verdict that `CommandVerifier` recorded during `run`. |
| `metrics` | The run's metrics: tokens, cost, LLM calls, tool calls, files changed, wall time, safety counts. |

`verify` does not re-run anything for coding: the worktree exists only during the cell, and
keeping the pipeline byte-for-byte unchanged was a requirement. The verdict is still independent:
hidden tests are injected after the agent exits, in an environment without provider credentials.
Splitting run and verify for coding is a later step (below).

### What does not change

The CLI commands (`run`, `sweep`, `ablate`, `grow`, `suite`, `experiment`, `harness`, `serve`,
`init`, `doctor`), every YAML format, the database schema and the dashboard stay as they are. The
sprint is additive: a new `harnesslab study` command group, the new packages, and one move: the
pure statistics helpers (`bootstrap_mean_diff`, `exact_sign_test`, `Interval`) move into the core
and `experiments/stats.py` re-exports them, so every existing import and result stays identical.

## Migration path

1. **This sprint (phase 0).** `harnesslab.study` and the contract; the `coding`, `qiskit` and
   `lean` adapters; the import-boundary test; contract tests run against all three adapters with
   fakes (no network, no API keys); the coding adapter runs the bundled fake suite through the
   contract and must reproduce the verdicts the existing pipeline gives for the same cells.
2. **Phase 1 (after the sprint).** `sweep` becomes a study over the coding adapter, and
   `holdout_tasks` gets sealed semantics (with the old post-hoc mode kept behind an explicit
   flag for one release); `ablate` becomes a core operation over knob levels ("each level against
   its absence") so it works for any adapter. Storage gains a generic `attempts` table keyed by
   study, task, config hash and repetition, with the adapter's payload as JSON; the dashboard
   reads studies from it.
3. **Phase 2.** `grow` becomes a proposal-based search strategy in the core with an
   adapter-supplied proposer, using the core gate and the sealed final split. The coding run
   pipeline is split into `run` (agent and diff, worktree kept) and `verify` (hidden tests, then
   cleanup). Coding-specific columns move into the adapter payload, and `core/models.py` is split
   into a neutral part and the coding part.

## Consequences

- New domains need no change to the core, and every domain gets the sealed protocol, the budget
  gate and the same statistics.
- Until phase 1 there are two ways to search coding configurations (`sweep` with its post-hoc
  holdout, and `study`), and studies do not appear in the dashboard.
- The coding adapter's `verify` cannot be called on its own until phase 2.

## Alternatives considered

- **Optional fields on `TaskSpec` and `VariantSpec`.** Rejected: puts proofs and circuits into
  the coding models and leaves every verifier dependent on a worktree and a diff.
- **Math and quantum as `HarnessRunner`s over a throwaway git repository.** Rejected: it would work
  mechanically (the verifier becomes a shell command), but the core would stay coding-shaped, and
  "hidden tests", diffs and worktrees would mean nothing for a proof or a circuit.
- **A separate repository or package per adapter.** Deferred: one repository is faster to change
  during the sprint, and optional extras keep the dependencies out of a plain install.

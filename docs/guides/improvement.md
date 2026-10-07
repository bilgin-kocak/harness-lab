# Improvement tasks

> **New in Harness Lab 0.2.0.** Upgrade an older install with `pip install -U harnesslab`.

Most coding benchmarks ask whether an agent can finish a task once. An **improvement task** asks
something harder and closer to engineering work: the repository already works, so can the agent
keep making it *better* on a measured objective, round after round, without breaking it?

The idea comes from speedrun-style evaluation of agents, where finishing is a given and the
question is how much better the agent's route gets with repeated attempts and a cheap evaluator in
the loop. Harness Lab makes the same measurement for code: runtime, operation counts, binary
size, memory, anything a command can print as a number.

```bash
harnesslab run demo-improve --variants fake-improver,fake-noop,fake-breaker   # seconds, no keys
harnesslab run demo-improve --variants claude-evaluator,claude-blind          # real Claude Code
harnesslab sweep run improve-budget --dry-run                                 # evaluator budget x model
```

## The protocol

For every run of an improvement task:

1. **Baseline.** Harness Lab copies the untouched worktree to a scratch directory, injects the
   hidden tests and runs the verification command (the *gate*). The repository must pass; a task
   whose baseline fails the gate is broken and the run is not verified. Then it injects the
   objective's files and runs the objective command; the printed number is the baseline. It must
   not be negative, and a minimized objective must not already be 0: improvement is scored as a
   ratio.
2. **Rounds.** The harness runs once per round. Its prompt is the task prompt plus a round
   section: the objective and its direction, the baseline, the best value so far, the result of
   every earlier round, the target if there is one, and whether it can measure the objective
   itself. Each round gets the full `agent_timeout_seconds`, so a run can take up to
   `rounds × agent_timeout_seconds` of agent time, plus an evaluation after each round.
3. **Evaluation.** After each round, Harness Lab evaluates a *copy* of the worktree the same way
   as the baseline. Hidden tests are only ever copied into that scratch copy, never into the
   agent's own worktree. A state that cannot be evaluated at all (a file the copy cannot read,
   say) counts as a failed round.
4. **Keep the best.** A round that passes the gate and beats the best so far becomes the new best
   checkpoint, a commit anchored by a ref so that `git gc` cannot prune it. With
   `keep_best: true` (the default), any other round is reverted to the best checkpoint before the
   next round, and the prompt says so. With `keep_best: false` the agent's changes carry over
   whatever happened. A revert restores tracked and untracked files but, like `git clean -fd`,
   leaves gitignored files alone. If taking a checkpoint or reverting fails, the protocol stops
   there: the run is marked `crashed`, keeps every round's usage and history, and is verified on
   the state the worktree actually holds.
5. **Verdict.** The usual final capture and verification run on the resulting worktree. The run
   **passes** when the final state passes the gate *and* the final value beats the baseline by
   more than `min_improvement`.

```text
baseline ── round 1 ── evaluate ── better? keep : revert ── round 2 ── … ── final verification
   65440        880        ✔ kept          440  ✔ kept              440  ✔ pass, 148.7× better
```

## Writing one

An improvement task is an ordinary task with an `improve:` section:

```yaml
id: fewer-key-operations
name: Find duplicates with fewer key operations
repo: { path: ../fixture_repo }
prompt: |
  `dedupe.first_duplicates` is correct but compares keys pairwise. Harness Lab measures how many
  key operations it performs on a fixed list of a few hundred keys. Fewer is better. Keep the
  behaviour identical. Do not modify anything under `tests/`.
verification:                              # the correctness gate, run on every evaluation
  command: python -m unittest discover -s tests -v
  inject:
    - { source: fewer-key-operations/verify/test_hidden_dedupe.py, dest: tests/test_hidden_dedupe.py }
  protected_paths: [tests/]
improve:
  objective:
    command: python .harnesslab_objective/bench.py   # prints one number
    inject:                                           # copied in only to measure
      - { source: fewer-key-operations/objective/bench.py, dest: .harnesslab_objective/bench.py }
    direction: minimize                               # minimize | maximize
    unit: key operations
    target: 440                                       # optional reference value
    repeats: 1                                        # median of this many measurements
  rounds: 3
  keep_best: true
  min_improvement: 0.0                                # relative: 0.05 = 5% better than baseline
  evaluator: { budget: 2 }                            # in-loop measurements per round; 0 = none
reference_solution:
  improve_overlays: [fewer-key-operations/round1, fewer-key-operations/round2]
```

The objective command must print the value as the last number on stdout, or as a JSON object with
a `value` key on its own line, and exit 0. Prefer deterministic objectives such as operation counts
or instruction counts; when the objective is noisy, such as wall time, use `repeats` and a
`min_improvement` above the noise. Hide the objective's implementation with `objective.inject`
when knowing it would let an agent game it, and protect the files it depends on with
`protected_paths`.

`harnesslab suite check` works on improvement tasks: the untouched repository must pass the gate
but not improve, and the reference overlays applied round by round must improve.

## The in-loop evaluator

With `evaluator.budget` above zero, Harness Lab installs `.harnesslab_eval/evaluate.py` in the
agent's worktree, with a copy of the objective's files under `.harnesslab_eval/objective/`, all
excluded from git so it never shows up in the diff. The prompt tells the agent it may run
`python .harnesslab_eval/evaluate.py` at most that many times per round. Each call copies the
working tree to a scratch directory, adds the objective's files there, measures and prints the
value. It does not run the correctness checks: the agent has its visible tests for that.

An agent that can measure the objective can also read how it is measured; it never learns where
the suite, and its hidden tests, live. Calls are counted from the evaluator's own log, written
before each measurement so that an interrupted call still counts, and from the trace, and
reported as `evaluator_calls`. The budget is cooperative, like the rest of Harness Lab's
isolation: an agent can run the measurement itself. When it does, the command shows up in the
trace.

## Comparing evaluator budget with model choice

Rounds and budget can be set per variant, so they work as sweep factors:

```yaml
# sweeps/improve-budget.yaml (bundled)
suite: demo-improve
base_variant: { runner: claude, max_turns: 30 }
factors:
  model: [claude-haiku-4-5, claude-sonnet-5]
  evaluator:
    none: { improve_eval_budget: 0 }
    few:  { improve_eval_budget: 2 }
    many: { improve_eval_budget: 10 }
baseline: { model: claude-haiku-4-5, evaluator: none }
```

`improve_rounds` overrides the number of rounds the same way. The paired comparison in the
compare view and in sweep reports includes an **improvement ratio** interval, so "does a bigger
evaluator budget help more than a bigger model?" gets an answer with an interval, not an anecdote.

## Fresh or resumed sessions

> **Unreleased.** On `main`; ships in the next release.

By default every round runs in a new agent session. Harness Lab owns the state between rounds
(the worktree, the best checkpoint, the history and the standing) and hands it to a fresh worker
in the full prompt. The variant option `improve_session` lets you test that design against the
alternative, one long conversation that continues across rounds:

```yaml
variants:
  - id: claude-resumed
    runner: claude
    improve_session: resume       # fresh (the default) | resume
```

| Round | `fresh` | `resume` |
| --- | --- | --- |
| 1 | New session, full prompt. | New session that the harness keeps, full prompt. |
| 2 and later | New session, full prompt with every earlier round. | Resumes the most recent session with a short delta prompt. |

The delta prompt holds only what the agent has not seen: the round heading, the result of the
previous round, the baseline and the best so far, the evaluator instructions when there is a
budget, and a closing instruction to keep improving. When `keep_best` reverted the previous round,
the agent's memory of its own edits is stale, so the delta prompt says explicitly that Harness Lab
reverted the files to the best version and asks the agent to read them again. If no earlier round
reported a session id (the harness failed before its session started, say), that round starts a
new session with the full prompt, and its `improve_history` note says so. Every round's prompt is
saved under `round-<n>/prompt.txt` in both modes.

Resume mode needs a runner that can resume a session: `claude`, `codex` and `fake`. With any
other runner, or an `improve_session` value other than `fresh` or `resume`, the run fails at setup
with a message naming the problem. Claude Code keeps the first round's session (no
`--no-session-persistence`) and later rounds pass `--resume <id>`; Codex runs later rounds as
`codex exec resume … <thread id> -`, which accepts no `profile`.

Things to keep in mind when you compare the two:

- **Transcripts live outside the sandbox.** A resumed session is stored where the CLI keeps its
  sessions, in your Claude Code or Codex configuration directory (`~/.claude`, or `~/.codex` and
  `CODEX_HOME`), not in the run's worktree. Harness Lab does not delete those transcripts, and
  its redaction applies to what it records, not to them.
- **Compare cost and cached tokens, not just input tokens.** A resumed round re-reads the whole
  conversation so far, mostly from the prompt cache. Its uncached `input_tokens` look small next
  to a fresh round's full prompt, while `cached_input_tokens` and the context the model works
  through keep growing. Cost and cached input tokens are the fair comparison.
- **Reverts happen under a resumed agent.** With `keep_best: true`, Harness Lab still restores
  the best checkpoint between rounds; the resumed agent learns it from the delta prompt, not from
  its own memory.

`improve.json` records the mode as `session`, the run's metrics as `improve_session`, and the
runner metadata's `improve_rounds` list each round's session id. The run page names the mode in
the improvement heading. To compare the modes with paired statistics, run both on the same tasks:

```bash
harnesslab run demo-improve --variants fake-improver,fake-improver-resumed   # seconds, no keys
harnesslab run demo-improve --variants claude-evaluator,claude-resumed       # real Claude Code
harnesslab sweep run improve-session --dry-run                               # fresh vs resume
```

The bundled `improve-session` sweep has one factor, `session` (`fresh` or `resume`), on Claude
Code with `fresh` as the baseline, so its report compares the two modes task by task, with the
paired improvement ratio interval described above.

## Reading the results

| Metric | Meaning |
| --- | --- |
| `improve_baseline`, `improve_best`, `improve_final` | Objective values: untouched repository, best checkpoint, final state. |
| `improve_ratio` | How many times better the final state is: baseline ÷ final when minimizing, final ÷ baseline when maximizing. Only reported when the final state passed verification, and only defined for positive values: a run that minimizes to 0 has no ratio (its score is 1). |
| `verified_score` | `1 − 1/ratio`: `0.5` for twice as good, `0.75` for four times, `1` for reaching 0 when minimizing. `0` without an improvement or when the gate fails. |
| `improve_progress` | Share of the way from the baseline to the `target`; `1.0` reached it, above `1.0` beat it. |
| `improve_curve` | Best value so far: the baseline, then after each round. This is the improvement curve. |
| `improve_history` | Per round: harness status, value, gate result, best so far, kept or reverted, evaluator calls, and a note (the harness's error, why the round could not be evaluated, or why the protocol stopped), redacted like everything else. |
| `evaluator_calls` | In-loop measurements the agent made across all rounds. |
| `improve_session` *(unreleased)* | `fresh` or `resume`: whether every round ran in a new agent session or the rounds continued one. |

The run page shows the rounds table and the improvement cards; the experiment page adds the median
improvement, the number of runs that passed by improving (including those without a finite ratio)
and evaluator calls per variant; `improve.json` in the run's artifacts holds the full
record, and every round's prompt is kept under `round-<n>/prompt.txt`.

Usage, cost and LLM calls are summed over all rounds, so a variant that improves more but spends
much more shows up on the cost axis as well. `agent_wall_time_seconds` covers the whole protocol:
the baseline, every round and every evaluation.

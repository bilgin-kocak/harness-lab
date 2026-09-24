# Research principles

The design follows recent work on agent harnesses (HarnessDev, *An Empirical Study of Harness
Design for Coding Agents*, *How Do Agent Harnesses Create Value?*, multi-harness RL, LoopArena
and *Grow the Harness, Not the Context*, among others). Six principles shape every feature.

## 1. Model and harness are independent variables

Most published comparisons conflate the two. Every run records the model requested and the model
resolved, the harness and its CLI version, the configuration hash, the harness bundle hash and the
environment hash separately, so you can ask whether a gain came from a better model or a better
scaffold.

## 2. Prefer component ablations over framework-versus-framework comparisons

Variants carry `context_policy`, `tool_policy`, `skill_version`, a harness bundle and arbitrary
options, so the same harness can be compared with one component changed. Sweeps report marginal
factor effects for exactly this reason.

## 3. Verification is external

The agent's "done" is recorded as a message, never as a result. Hidden tests are injected after
the agent exits, protected paths are checked before the verifier runs, and the verifier receives
no credentials. The fake no-op agent in the demo, which claims success and fails every task,
exists to make this visible.

## 4. Raw evidence is preserved

Normalized events with stable ids and ordering, diffs against the base commit, verifier output,
and sanitized provider streams are kept per run for replay and failure analysis. Aggregates never
hide individual runs.

## 5. Pareto frontiers, not one score

There is no composite "winner" metric. The dashboard shows verified success, cost, latency,
tokens, LLM calls and variance side by side, and sweeps report a Pareto front alongside their
recommendation.

## 6. Reproducibility

Task hash, prompt hash, base commit, Harness Lab commit, runner configuration, CLI version,
timestamps and platform are stored with every run. Plain-directory fixtures produce the same base
commit on every machine. Experiments and grow sessions export to JSON.

## What this rules out

By design, Harness Lab does not do prompt optimization of the *task* prompt, reinforcement
learning, LLM-as-judge scoring, accounts, teams, billing, distributed workers, Kubernetes, vector
databases or model routing. It is a local instrument for controlled comparisons.

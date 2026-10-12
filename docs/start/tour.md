---
hide:
  - toc
---

# Tour

Every image on this page comes from the bundled demo suites, run against the offline fake
agents. [`scripts/site_assets.py`](https://github.com/bilgin-kocak/harness-lab/blob/main/scripts/site_assets.py)
regenerates them in about two minutes without an API key. The tokens, costs and timings are
simulated: they show what Harness Lab records, not how any real agent performs. Click an image to
open it at full size.

## The first run

[![Terminal output of harnesslab run demo with the reference and no-op agents](../assets/terminal/run-demo.svg)](../assets/terminal/run-demo.svg)

Three tasks, two agents, one attempt each. The reference agent applies each task's known
solution; the no-op agent claims success and changes nothing. The hidden tests, not the agents,
decide which runs passed.

## Experiments

[![The dashboard's experiment list](../assets/screenshots/experiments.webp)](../assets/screenshots/experiments.webp)

`harnesslab serve` opens a local dashboard. Every experiment is listed with its suite, variants,
runs and how many passed.

## The task × variant matrix

[![The task × variant matrix of four fake agents with three attempts per cell](../assets/screenshots/experiment-matrix.png)](../assets/screenshots/experiment-matrix.png)

`harnesslab run demo --variants fake-reference,fake-noop,fake-flaky,fake-partial -r 3`: each
cell shows the passes out of its attempts and links to every run. `fake-flaky` breaks the code,
fixes it and changes nothing in turn; `fake-partial` solves only the bug-fix task.

## Several attempts: pass@k and best-of-k

[![pass@k and best-of-k per variant](../assets/screenshots/attempts.webp)](../assets/screenshots/attempts.webp)

pass@3 is the chance that one of three attempts passes. best-of-3 is the chance that the attempt
picked by the task's visible check passes the hidden tests. `fake-flaky` reaches pass@3 of 100%
but best-of-3 of only 50%: its attempt that changes nothing passes the visible tests and fails the
hidden ones.

## Two variants, paired by task

[![The compare view: raw metrics side by side and the paired statistical evidence](../assets/screenshots/compare.webp)](../assets/screenshots/compare.webp)

Raw metrics side by side, with no composite score, then the evidence: per-task differences with
bootstrap intervals and a sign test. With three tasks no verdict is given; a verdict needs at
least five paired tasks.

## One run: metrics, safety and timeline

[![A run page: metric cards, safety verdict, final message and the normalized timeline](../assets/screenshots/run.webp)](../assets/screenshots/run.webp)

The verified result, duration, tokens, cost, tool calls, files changed and exit codes; the safety
verdict; the agent's final message, which is not evidence of success; and the normalized
timeline. Reasoning content is never recorded.

## One run: diff and verifier

[![A run's captured diff and the verifier's record](../assets/screenshots/run-diff.webp)](../assets/screenshots/run-diff.webp)

The diff Harness Lab captured against the base commit, and the verifier's record: its command and
exit code, the visible check run before the hidden files were injected, the partial score, and
the files injected at verification.

## Cheapest verified configuration

[![A sweep's recommendation for each task](../assets/screenshots/sweep.webp)](../assets/screenshots/sweep.webp)

`harnesslab sweep run demo-fake` crosses behaviour × simulated price × action granularity: eight
configurations, three tasks, two attempts each. For every workload it recommends the cheapest
configuration that passes.

[![Every configuration of one workload with its pass rate, cost and Pareto front](../assets/screenshots/sweep-configs.webp)](../assets/screenshots/sweep-configs.webp)

Every configuration with its pass rate, cost, tokens and wall time. ✔ marks the eligible ones and
◆ the Pareto front of pass rate against cost.

## Grow sessions

[![The list of grow sessions](../assets/screenshots/grow-list.webp)](../assets/screenshots/grow-list.webp)

[![A grow session's versions with their gate results](../assets/screenshots/grow.webp)](../assets/screenshots/grow.webp)

`harnesslab grow run demo-fake` lets an optimizer edit a harness bundle. Version v1 was accepted:
it fixed both failing window tasks without lowering the held-out gate. The gate stays at 0%
because the demo's fake optimizer only memorizes the tasks it saw fail, which is exactly what a
held-out gate is there to expose.

## Component ablation

[![Component ablation: one component helps, the other shows no evidence](../assets/screenshots/ablation.webp)](../assets/screenshots/ablation.webp)

A synthetic six-task suite and a two-part bundle: a skill that solves every task and a system
prompt that only costs LLM calls. The ablation finds that the skill helps (six wins out of six,
sign test p = 0.031) and gives no evidence for the prompt, which adds three LLM calls per run.

## Safety next to success

[![A run's safety findings: every risky action with its rule and outcome](../assets/screenshots/safety.webp)](../assets/screenshots/safety.webp)

In the `demo-safety` suite, `fake-reckless` reads a canary file, pipes a lure URL into a shell and
leaks a planted token. Every risky action is listed with its rule, its source and whether anything
blocked it. The bundled sentinel hook can be A/B tested against it.

## Improvement rounds

[![Improvement rounds against a measured objective](../assets/screenshots/improvement.webp)](../assets/screenshots/improvement.webp)

In `demo-improve` the agent works in rounds against a measured objective. Every round is evaluated
on a copy with the hidden checks, and a round that does not beat the best so far is reverted. The anytime score
also credits how early the improvements came.

## Recovery from an injected fault

[![Recovery: success with and without an injected fault](../assets/screenshots/recovery.webp)](../assets/screenshots/recovery.webp)

`demo-recovery` pairs a task that has an injected fault with its fault-free twin. `fake-careful`
recovers every time; `fake-naive` retries blindly and pays twice.

## Reports in the terminal

[![Terminal output of harnesslab experiment compare](../assets/terminal/experiment-compare.svg)](../assets/terminal/experiment-compare.svg)

[![Terminal output of harnesslab grow show](../assets/terminal/grow-show.svg)](../assets/terminal/grow-show.svg)

[![Terminal output of harnesslab ablate report](../assets/terminal/ablate-report.svg)](../assets/terminal/ablate-report.svg)

Every dashboard view has a terminal counterpart, and every experiment exports to JSON.

## Next

Install Harness Lab ([Install](install.md)) and run the same demo yourself
([Quickstart](quickstart.md)), then read the [Concepts](concepts.md).

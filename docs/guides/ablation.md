# Ablate a harness

> **Requires Harness Lab 0.2.0 (unreleased).** `pip install harnesslab` currently installs 0.1.0,
> which has no `ablate` command and no harness bundles. Until 0.2.0 is on PyPI, install from `main`:
> `pip install git+https://github.com/bilgin-kocak/harness-lab`.

A harness component earns its place only if the agent does better *with* it than *without* it, on
the same tasks, by more than noise. Recent work shows this is not a given: with a strong model,
elaborate harnesses can add nothing over a minimal agent with a shell and a file system
(arXiv 2609.40303), while task-adapted components sometimes help a lot (arXiv 2609.40330,
2609.38372). `harnesslab ablate` measures it for your bundle and your tasks.

## Run an ablation

```bash
harnesslab ablate run harnesses/grown --suite my-suite --variant claude-default --repetitions 2
harnesslab ablate run harnesses/grown --suite my-suite --variant claude-default --dry-run   # plan only
harnesslab ablate report <experiment-id>                                                   # recompute
```

From one bundle it builds:

| Variant | Bundle |
| --- | --- |
| `full` | the bundle as is |
| `minimal` | no components: only `harness.yaml` and `fake.yaml` (metadata and simulation, not harness) |
| `without:<component>` | the bundle minus one component |

Components are `system_prompt.md`, each `skills/<name>`, each `agents/<name>.md` and `hooks.json`.
All variants share the base variant's runner, model and options and run as one ordinary
experiment, so the matrix, run pages and compare view work as usual.

## Read the report

```
whole bundle (full vs minimal): better over 6 paired task(s) (wins 6, losses 0, ties 0, sign test p = 0.031)
  pass rate (pts): +100.0 [+100.0, +100.0] 95% interval, P(>0) 100%
  total_tokens: +0.0000 [+0.0000, +0.0000] 95% interval, P(>0) 0%
  llm_calls: +5.00 [+5.00, +5.00] 95% interval, P(>0) 100%

 component         verdict      Δ pass (pts)          interval   Δ cost  Δ llm_calls  W/L/T
 system_prompt.md  no evidence          +0.0      [+0.0, +0.0]  +0.0000        +3.00  0/0/6
 skills/careful    helps              +100.0  [+100.0, +100.0]  +0.0000        +2.00  6/0/0
```

This is the offline example from the test suite (fake runner, six tasks): the skill solves every
task, while the system prompt changes nothing except three extra LLM calls per run, so it should be
dropped. Cost falls back to total tokens when no run reported a cost.

A component's effect is *full minus without-component*, task by task:

- **helps**: the interval of the pass-rate difference lies entirely above zero;
- **hurts**: entirely below zero;
- **no evidence**: the interval contains zero. Prefer the simpler harness, especially when the
  component costs tokens or LLM calls (the cost columns show it);
- **not enough tasks**: fewer than `--min-tasks` (default 5) paired tasks. Repetitions reduce noise
  within a task but do not add tasks, so three tasks never yield a verdict.

The same report is on the experiment's dashboard page and in its JSON export (`ablation_report`).
How the intervals are computed is in [Read and export results](results.md#statistics).

## Simulate it without API keys

The fake runner reads two keys from a bundle's `fake.yaml` that make ablations testable offline:
`component_solves: {skills/<name>: [task ids]}` solves those tasks only while the component is in
the bundle, and `component_llm_calls: {<component>: n}` adds simulated LLM calls when it is.

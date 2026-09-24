# Harness Lab

**A local-first experimentation platform for evaluating AI coding-agent harnesses.**

Given the same software task and the same starting repository state, how do different agent
harnesses and configurations compare in *verified* success, token usage, tool usage, latency,
cost and behaviour? Harness Lab answers that question reproducibly:

```text
                 TASK SUITE
                     │
            identical starting repo (one git commit)
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
       Harness A  Harness B  Harness C        codex · claude · fake · generic · yours
          │          │          │
      isolated    isolated    isolated        one git worktree per run
      worktree    worktree    worktree
          │          │          │
          └──────────┼──────────┘
                     ▼
              independent verifier            hidden tests + exit code / partial score
                     │
                     ▼
            normalized experiment             provider-neutral trace + metrics + diff
                     │
                     ▼
              Harness Lab UI                   matrix · run detail · compare · grow
```

Harness Lab is **not** an observability product. It is a scientific instrument for controlled
comparisons: the agent never decides whether it succeeded, every run starts from the same commit,
and every trace is stored in one provider-neutral schema.

## What it does

- **Runs the same task through different harnesses.** Claude Code, the OpenAI Codex CLI, a
  deterministic fake agent, any CLI you wrap, or a Python adapter you write.
- **Decides success independently.** Hidden tests are injected after the agent exits and a
  verifier command produces the verdict.
- **Normalizes every trace.** Tool calls, commands, file changes, tokens, cost and LLM calls are
  stored the same way for every harness. Hidden reasoning is never persisted.
- **Records what reproduction needs.** Base commit, task hash, harness bundle hash, config hash,
  CLI version, model and environment on every run.
- **Compares configurations.** Sweeps search model × effort × toolset × compaction × action policy
  × harness bundle for the cheapest configuration that still passes.
- **Grows a harness from its failures.** The `grow` loop edits a harness bundle with an optimizer,
  keeps only edits that fix failures without regressing a held-out gate, and records the lineage.

## Where to go

| You want to | Read |
| --- | --- |
| Install and see it work in five minutes without any API key | [Install](start/install.md), then [Quickstart](start/quickstart.md) |
| Understand runs, experiments, sweeps and grow sessions | [Concepts](start/concepts.md) |
| Benchmark Claude Code or Codex | [Run Claude Code](guides/claude-code.md), [Run Codex](guides/codex.md) |
| Plug in your own agent | [Test your own harness](guides/your-own-harness.md) |
| Build a benchmark of your own tasks | [Write a task suite](guides/task-suites.md) |
| Find the cheapest configuration that still passes | [Configuration sweeps](guides/sweeps.md) |
| Improve a harness automatically with a regression gate | [Grow the harness](guides/growing.md) |
| Look up a flag, a YAML field or a metric | [Reference](reference/cli.md) |
| Know what is deliberately not solved | [Honest limits](explanations/limits.md) |

## Requirements

Python 3.12 or newer, git 2.20 or newer, Linux or macOS (Windows through WSL). Nothing else is
needed for the demo: the fake runner needs no API keys. The Claude Code and Codex adapters need
their CLIs on `PATH` and their credentials.

Source and issues: [github.com/bilgin-kocak/harness-lab](https://github.com/bilgin-kocak/harness-lab).
Package: [pypi.org/project/harnesslab](https://pypi.org/project/harnesslab/). License: MIT.

# Harness Lab

**A local-first experimentation platform for evaluating AI coding-agent harnesses.**

[![The Harness Lab dashboard: the task × variant matrix of the bundled demo, four offline fake agents with three attempts each](https://bilgin-kocak.github.io/harness-lab/assets/screenshots/experiment-matrix.png)](https://bilgin-kocak.github.io/harness-lab/)

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

**Website and documentation: <https://bilgin-kocak.github.io/harness-lab/>** (with a [tour](https://bilgin-kocak.github.io/harness-lab/start/tour/) of every view)

## Install

Python 3.12 or 3.13, git 2.20+, Linux or macOS (Windows through WSL).

```bash
pip install harnesslab            # or: pipx install harnesslab  /  uv tool install harnesslab
harnesslab version && harnesslab doctor
```

## Five minutes, no API keys

```bash
harnesslab run demo --variants fake-reference,fake-noop   # bundled suite, two fake agents
harnesslab init my-lab && cd my-lab                       # editable copies of the suites, sweeps, harnesses
harnesslab sweep run sweeps/demo-fake.yaml                # cheapest verified configuration
harnesslab grow run grow/demo-fake.yaml                   # grow a harness from failures
harnesslab serve                                          # dashboard at http://127.0.0.1:8000 (Ctrl-C stops it)
```

A real harness uses real API usage, so Harness Lab says how many runs of which harness will start
and, in a terminal, asks first (`--yes` skips the question):

```bash
harnesslab run suites/demo/suite.yaml --variants claude-default   # needs the claude CLI
```

## What it does

- **Runs the same task through different harnesses**: Claude Code, the OpenAI Codex CLI, a
  deterministic fake agent, any CLI you wrap, or a Python adapter you write.
- **Decides success independently**: hidden tests are injected after the agent exits and a
  verifier command produces the verdict.
- **Normalizes every trace**: tool calls, commands, file changes, tokens, cost and LLM calls in one
  schema. Hidden reasoning is never persisted.
- **Records what reproduction needs**: base commit, task hash, harness bundle hash, config hash,
  CLI version, model and environment on every run.
- **Compares configurations**: sweeps search model × effort × toolset × compaction × action policy
  × harness bundle for the cheapest configuration that still passes.
- **Measures improvement, not just completion**: improvement tasks give the agent several rounds
  to make a working repository better on a measured objective, with an optional in-loop
  evaluator, and report the improvement curve.
- **Measures safety next to success**: every run gets risky-action findings, executed or blocked,
  and tasks can plant canary secrets and prompt-injection lures; a bundled sentinel hook can be
  A/B tested.
- **Grows a harness from its failures**: the `grow` loop edits a harness bundle with an optimizer,
  keeps only edits that fix failures without regressing a held-out gate, and records the lineage
  (after *Grow the Harness, Not the Context*, arXiv 2609.26760).
- **Mines tasks from your repository**: `suite mine` turns commits that changed code and tests
  into verifier-backed tasks (fail at the parent, pass at the commit), so comparisons run on dozens
  of tasks from your own code.
- **Proves each component earns its place**: `ablate` tests every part of a harness bundle against
  its own absence, and every comparison carries paired, task-level confidence intervals.
- **Counts attempts, discovery and recovery**: pass@k and best-of-k over repetitions, tasks that
  grade everything an agent found against a hidden answer key, and tasks with an injected fault
  reported against their fault-free twin.

## Guides

| Topic | Read |
| --- | --- |
| Architecture: runs, experiments, sweeps, grow sessions | [Concepts](https://bilgin-kocak.github.io/harness-lab/start/concepts/) |
| The fake demo, step by step | [Quickstart](https://bilgin-kocak.github.io/harness-lab/start/quickstart/) |
| Running Claude Code | [Run Claude Code](https://bilgin-kocak.github.io/harness-lab/guides/claude-code/) |
| Running Codex | [Run Codex](https://bilgin-kocak.github.io/harness-lab/guides/codex/) |
| Creating a task suite | [Write a task suite](https://bilgin-kocak.github.io/harness-lab/guides/task-suites/), [Mine tasks from git history](https://bilgin-kocak.github.io/harness-lab/guides/task-corpus/) |
| Creating a harness variant | [Task and suite YAML: variants](https://bilgin-kocak.github.io/harness-lab/reference/task-and-suite-format/#variant), [Test your own harness](https://bilgin-kocak.github.io/harness-lab/guides/your-own-harness/) |
| Interpreting metrics | [Metrics](https://bilgin-kocak.github.io/harness-lab/reference/metrics/), [Read and export results](https://bilgin-kocak.github.io/harness-lab/guides/results/) |
| Sweeps, grow, ablation | [Configuration sweeps](https://bilgin-kocak.github.io/harness-lab/guides/sweeps/), [Grow the harness](https://bilgin-kocak.github.io/harness-lab/guides/growing/), [Ablate a harness](https://bilgin-kocak.github.io/harness-lab/guides/ablation/) |
| CLI and YAML reference | [Reference](https://bilgin-kocak.github.io/harness-lab/reference/cli/) |
| Roadmap | [Roadmap](https://bilgin-kocak.github.io/harness-lab/project/roadmap/) |

The same pages live in [`docs/`](https://github.com/bilgin-kocak/harness-lab/tree/main/docs) and
can be read on GitHub, starting from the
[overview](https://github.com/bilgin-kocak/harness-lab/blob/main/docs/start/overview.md).

## Security warning

Harness Lab **executes coding agents and agent-written code on your machine**, isolated with git
worktrees, not containers. Use trusted benchmark repositories and throwaway credentials.
Redaction of secrets in traces and logs is a basic pattern filter, not a formal data-loss
prevention system. Read the
[security model](https://bilgin-kocak.github.io/harness-lab/explanations/security-model/) first.

## Developing

```bash
git clone https://github.com/bilgin-kocak/harness-lab && cd harness-lab
uv sync && uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests
uv run mkdocs build --strict
```

Changelog: [CHANGELOG.md](https://github.com/bilgin-kocak/harness-lab/blob/main/CHANGELOG.md).
License: MIT.

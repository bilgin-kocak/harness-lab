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

**Documentation: <https://bilgin-kocak.github.io/harness-lab/>**

## Install

Python 3.12+, git 2.20+, Linux or macOS (Windows through WSL).

```bash
pip install harnesslab            # or: pipx install harnesslab  /  uv tool install harnesslab
harnesslab doctor
```

## Five minutes, no API keys

```bash
harnesslab run demo --variants fake-reference,fake-noop   # bundled suite, two fake agents
harnesslab serve                                          # http://127.0.0.1:8000
harnesslab init my-lab && cd my-lab                       # editable copy of the demo, sweeps, harnesses
harnesslab sweep run sweeps/demo-fake.yaml                # cheapest verified configuration
harnesslab grow run grow/demo-fake.yaml                   # grow a harness from failures
harnesslab run suites/demo/suite.yaml --variants claude-default   # a real harness (needs the claude CLI)
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
- **Grows a harness from its failures**: the `grow` loop edits a harness bundle with an optimizer,
  keeps only edits that fix failures without regressing a held-out gate, and records the lineage
  (after *Grow the Harness, Not the Context*, arXiv 2609.26760).

## Read more

| | |
| --- | --- |
| Quickstart and concepts | [Start here](https://bilgin-kocak.github.io/harness-lab/start/quickstart/) |
| Claude Code, Codex, your own harness | [Guides](https://bilgin-kocak.github.io/harness-lab/guides/claude-code/) |
| Task suites, sweeps, growing the harness | [Guides](https://bilgin-kocak.github.io/harness-lab/guides/task-suites/) |
| CLI, YAML formats, metrics, Python API | [Reference](https://bilgin-kocak.github.io/harness-lab/reference/cli/) |
| Security model and honest limits | [Explanations](https://bilgin-kocak.github.io/harness-lab/explanations/security-model/) |
| Contributing and releasing | [Project](https://bilgin-kocak.github.io/harness-lab/project/contributing/) |

The same pages live in [`docs/`](docs/) and can be read on GitHub.

## Security warning

Harness Lab **executes coding agents and agent-written code on your machine**, isolated with git
worktrees, not containers. Use trusted benchmark repositories and throwaway credentials. Read the
[security model](https://bilgin-kocak.github.io/harness-lab/explanations/security-model/) first.

## Developing

```bash
git clone https://github.com/bilgin-kocak/harness-lab && cd harness-lab
uv sync && uv run pytest && uv run ruff check src tests && uv run mkdocs build --strict
```

Changelog: [CHANGELOG.md](CHANGELOG.md). License: MIT.

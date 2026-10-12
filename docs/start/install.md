# Install

## Requirements

- Python 3.12 or 3.13
- git 2.20 or newer
- Linux or macOS. Native Windows is not supported (the runner relies on POSIX process groups and
  git worktrees); use WSL.

The demo needs nothing else. To benchmark real harnesses you also need:

- the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) on `PATH` with
  `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or a completed `claude auth login`;
- the [OpenAI Codex CLI](https://github.com/openai/codex) on `PATH` with `OPENAI_API_KEY` or a
  completed `codex login`;
- *(unreleased)* [pi](../guides/pi.md), [OpenCode](../guides/opencode.md) or
  [Hermes Agent](../guides/hermes.md) on `PATH`, with an API key for your model's provider
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, ...) in the environment.

## Install the package

```bash
pip install harnesslab
# or
pipx install harnesslab
# or
uv tool install harnesslab
```

Pages marked *New in Harness Lab X.Y.Z*, and options or fields marked *(X.Y.Z)*, need that
version or later; check yours with `harnesslab version` and upgrade with
`pip install -U harnesslab`. To try changes on `main` before they are released:

```bash
pip install git+https://github.com/bilgin-kocak/harness-lab
```

Then check the version and the environment:

```bash
harnesslab version
harnesslab doctor
```

`doctor` reports Python, git, the harness CLIs (Codex, Claude Code and, unreleased, pi, OpenCode
and Hermes Agent), `uv` and the database. Missing CLIs are a warning, not an error: they only
disable their runners. A CLI that is installed but no
longer accepts a flag Harness Lab passes shows as `incompatible`, with the flag *(0.3.0)*.

## Where data goes

Everything Harness Lab writes lives under one directory, by default `./.harnesslab` in the
current working directory: the SQLite database, fixture snapshots, worktrees, artifacts and grow
sessions. Override it with `HARNESSLAB_HOME` or `--home` on any command. Deleting that directory
removes all state. See [Storage and data layout](../reference/storage.md).

## Develop Harness Lab itself

```bash
git clone https://github.com/bilgin-kocak/harness-lab
cd harness-lab
uv sync                     # installs the package, the dev tools and the docs tools
uv run pytest
uv run harnesslab doctor
uv run mkdocs serve         # this documentation at http://127.0.0.1:8000
```

See [Contributing](../project/contributing.md).

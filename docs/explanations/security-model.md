# Security model

Harness Lab **executes coding agents and agent-written code on your machine**. Read this page
before pointing it at anything you care about.

## Isolation is git worktrees, not containers

- Agents run with your user's privileges. The Codex adapter uses Codex's own `workspace-write`
  sandbox with network disabled; the Claude adapter uses `acceptEdits` plus a shell allowlist and
  denies everything else. Neither is a security boundary against a determined agent.
- Use only trusted benchmark repositories and throwaway credentials until container isolation
  (`DockerSandbox`, on the [roadmap](../project/roadmap.md)) exists.
- Git fixture repositories are copied into Harness Lab's home with no remote, so nothing an agent
  does, not even `git push`, reaches your source. Only the base commit and its ancestors are
  copied, so later commits (for a [mined task](../guides/task-corpus.md), the answer) are not in
  the agent's repository at all.
- `harnesslab suite mine` runs the mined repository's tests and `--setup` commands on your
  machine while validating; mine only repositories you trust.

## Hidden tests

Hidden tests are copied into the worktree only at verification time, and visible tests under
`protected_paths` fail the run if changed. An agent that searches the host filesystem could still
find `suites/<suite>/tasks/*/verify`; runs whose shell commands mention the suite directory are
flagged with `possible_suite_access` in the run metadata.

The verifier runs in the same worktree the agent just edited. Protected paths cover the visible
tests, but an agent could in principle tamper with the package under test to subvert the test
runner. Keep hidden tests independent of the code they test where you can, and read the diff of
any surprising pass.

## Credentials and environment

The environment passed to agents is an allowlist: `PATH`, locale, proxy and CA settings, and
provider credentials. Verifier and setup commands receive no credentials at all. Environment
variables are never logged. The full list is in [Storage and data layout](../reference/storage.md).

## Redaction before persistence

Command output, messages, tool output, diffs and stderr pass through a heuristic redactor:
provider `sk-*` keys, GitHub tokens, bearer tokens, AWS keys, Slack and Google keys, `NAME=value`
assignments for secret-looking names, private-key blocks, and the literal values of secret-looking
variables in Harness Lab's own environment. **This is a safety net, not a data-loss-prevention
system**; anything it misses is stored verbatim.

## Hidden reasoning

Hidden chain-of-thought is never persisted or rendered: thinking and reasoning content is
dropped at parse time and only `reasoning_event {count}` metadata survives. The stored provider
stream is a sanitized copy. The test suite scans the database and every artifact for leaked
markers.

## Harness bundles and hooks

A bundle's `hooks.json` is materialized as a Claude Code plugin, and Claude Code runs hook
commands on your machine with your privileges, outside the agent's tool allowlist. A hook can do
anything you can, including reading a suite's hidden tests from disk. Treat a bundle with hooks
like a script you are about to run; `harnesslab harness check` warns when a bundle has one.

## The optimizer

A grow session gives an LLM optimizer a view of failing runs. That view never contains hidden
test sources, names or quoted lines, and by default not the expected values that assertions
print; every candidate bundle is linted before it runs. An optimizer may not write `hooks.json`
unless the grow spec sets `optimizer.allow_hooks: true`, because otherwise LLM-written shell
commands would run on your machine unreviewed. The `claude-cli` optimizer runs with no tools in an
empty temporary directory, which is removed afterwards, so it can neither read your files nor pick
up your project's CLAUDE.md and hooks. See [What the optimizer sees](optimizer-view.md).

## Runaway processes

Agents and verifiers are killed as a process group (SIGTERM, grace period, SIGKILL) after their
timeouts. Interrupting Harness Lab marks in-flight runs `interrupted`; grow sessions save their
state and can be resumed.

## The dashboard

`harnesslab serve` has no authentication and binds to `127.0.0.1` by default. Do not expose it
on a shared network without putting something in front of it.

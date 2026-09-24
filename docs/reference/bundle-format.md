# Harness bundle

A harness bundle is the growable outer layer of a coding-agent harness: a plain directory that a
variant carries with `harness: <path>` and that a grow session edits.

```text
harnesses/baseline/
  harness.yaml         optional manifest: name, description (never edited by an optimizer)
  system_prompt.md     appended to the agent's system prompt
  skills/<name>/SKILL.md   YAML frontmatter with `name:` required; other files under skills/ allowed
  hooks.json           Claude Code hooks, plugin format: {"hooks": {...}}
  agents/<name>.md     Claude Code custom agents
  fake.yaml            simulation only: solve_tasks, fail_tasks, llm_calls (read by the fake runner)
```

## Rules

- Allowed paths: the five top-level files above and anything under `skills/` or `agents/`.
  No other files, no symlinks, no `..`.
- Caps: 64 000 bytes per file, 512 000 bytes per bundle, 64 files.
- `hooks.json` must be JSON with a top-level `hooks` object; `fake.yaml` and `harness.yaml` must
  be YAML mappings; every `skills/<name>/SKILL.md` starts with frontmatter containing `name`.
- Hidden files and caches (`.DS_Store`, `__pycache__`, dotfiles) are ignored on load.
- An empty directory is a valid bundle: the "strategy-free scaffold".

`harnesslab harness check <dir> [--suite <suite>]` validates a bundle and, with a suite, runs the
leak lint against it.

## The hash

`harness_hash` is a SHA-256 over every file's relative path, size and bytes, sorted by path. It is
recorded on every variant and run that used the bundle, and it is part of the variant's
`config_hash`. The experiment service snapshots each bundle under
`.harnesslab/artifacts/<experiment>/harness/<hash>/`, and the variant record lists the files.

## How runners apply it

| Runner | `system_prompt.md` | `skills/`, `hooks.json`, `agents/` | `fake.yaml` |
| --- | --- | --- | --- |
| `claude` | written to a file with `append_system_prompt` and the action policy, passed as `--append-system-prompt-file` | materialized as a plugin directory (`.claude-plugin/plugin.json`, `skills/`, `hooks/hooks.json`, `agents/`) and passed as `--plugin-dir`; works under `--bare` | ignored |
| `codex` | prompt prefix, followed by each skill under `## Skill: <name>` | hooks and agents ignored, recorded in a `harness_components_ignored` event | ignored |
| `generic` | prepended to the prompt | available to the command through `{harness_dir}` and `HARNESSLAB_HARNESS_DIR` | ignored |
| `fake` | ignored | ignored | `solve_tasks` and `fail_tasks` override the variant's `behavior` per task (solve wins); `llm_calls` sets the simulated count |

## Optimizer edit rules

An optimizer may edit or add content files only. It may not delete a file, edit `harness.yaml`,
change more than `optimizer.max_files` files, exceed the caps, mention a suite task id or a hidden
test name, or copy a line of 24 characters or more verbatim from a hidden test. `fake.yaml` is
exempt from the task-id rule only, because real runners ignore it and the fake optimizer needs it.

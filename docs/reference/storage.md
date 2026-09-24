# Storage and data layout

Everything lives under one directory, by default `./.harnesslab` (override with
`HARNESSLAB_HOME` or `--home`). Delete the directory to delete all state.

```text
.harnesslab/
  harnesslab.db            SQLite (WAL mode)
  fixtures/<hash>/         materialized fixture repositories (plain directories → deterministic commit)
  repos/<hash>/            internal clones of git fixture repositories (origin removed)
  worktrees/<exp>/<run>/   one detached worktree per run, removed after the run unless kept
  artifacts/<exp>/<run>/   prompt.txt · agent.diff · diff_stat.txt · git_status.txt
                           verifier_stdout.txt · verifier_stderr.txt
                           agent_stream.sanitized.jsonl · agent.stderr.log   (real harnesses)
                           system_prompt.txt · plugin/                       (Claude with a bundle)
  artifacts/<exp>/harness/<hash>/   snapshot of each harness bundle used by the experiment
  grow/<session>/v<N>/bundle/       each grow version's bundle
  grow/<session>/v<N>/context.json  exactly what the optimizer saw
  grow/<session>/v<N>/proposal.json the raw proposal
  grow/<session>/v<N>-proposal/     the manual optimizer's working directory
  logs/
```

`HARNESSLAB_DB_URL` points the database elsewhere (any SQLAlchemy URL); SQLite is the tested
path. Schema changes are forward-only: new tables are created and new nullable columns are added
on start-up, so an older database keeps working.

## Tables

| Table | Holds |
| --- | --- |
| `experiments` | Name, suite, spec, status, repetitions, parallelism, timestamps, Harness Lab version and commit, environment and its hash, grow session id and role when part of a session. |
| `variants` | Per experiment: key, runner, model, description, recorded policies, configuration and `config_hash`, sweep factors, `harness_hash` and the bundle's file list. |
| `tasks` | Per experiment: key, name, version, `task_hash`, `spec_hash`, `prompt_hash`, base commit, repo path, tags, spec. |
| `runs` | One row per cell: status, outcome, timestamps, worktree, the reproducibility record (base commit, hashes, runner, configuration, environment, models, CLI version, session id), exit code, error, final message, runner metadata, `metrics_json` and denormalized metric columns including `llm_calls`. |
| `events` | Normalized events, unique on `(run_id, sequence)`. |
| `verifier_results` | Command, exit code, timeout, duration, capped stdout and stderr, score fields, protected-path violations, injected files, skip reason. |
| `artifacts` | Kind, path relative to home, media type, size, SHA-256. |
| `grow_sessions` | Spec, status, phase, state, initial and current version, iterations, timestamps, notes. |
| `harness_versions` | Number, parent, hash, bundle path, status, reason, window tasks and fixes, window and gate experiment ids, gate pass rate and counts, gate medians, optimizer kind, model, usage and cost, rationale. |

Large blobs (diffs, raw streams, verifier logs) are stored as files and referenced from
`artifacts`; the database keeps queryable metadata and capped text copies.

## Environment variables

| Variable | Effect |
| --- | --- |
| `HARNESSLAB_HOME` | Data directory. |
| `HARNESSLAB_DB_URL` | Database URL. |
| `HARNESSLAB_INTEGRATION=1` | Enables the opt-in real-CLI tests. |

Variables forwarded to agent processes: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`,
`LANGUAGE`, `TERM`, `TZ`, `TMPDIR`, `TMP`, `TEMP`, CA and proxy settings, `LC_*`, `XDG_*`, plus,
for agents only, `ANTHROPIC_*`, `CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR`, `AWS_*`,
`GOOGLE_APPLICATION_CREDENTIALS`, `CLOUD_ML_REGION`, `OPENAI_*`, `CODEX_HOME`, and whatever a
variant lists in `env_passthrough`. Every child also gets `NO_COLOR=1`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTHONUNBUFFERED=1`, `GIT_TERMINAL_PROMPT=0` and `HARNESSLAB=1`. Verifier and setup commands
receive no credentials. Environment variables are never logged.

# Storage and data layout

Everything lives under one directory, by default `./.harnesslab` (override with
`HARNESSLAB_HOME` or `--home`). Delete the directory to delete all state.

```text
.harnesslab/
  harnesslab.db            SQLite (WAL mode)
  fixtures/<hash>/         materialized fixture repositories (plain directories → deterministic commit)
  repos/<hash>-<commit>/   internal copies of git fixture repositories: base commit and ancestors, no remote
  worktrees/<exp>/<run>/   one detached worktree per run, removed after the run unless kept
  artifacts/<exp>/<run>/   prompt.txt · agent.diff · diff_stat.txt · git_status.txt
                           verifier_stdout.txt · verifier_stderr.txt · visible_check.txt
                           agent_stream.sanitized.jsonl · agent.stderr.log   (real harnesses)
                           codex_last_message.txt                            (Codex)
                           system_prompt.txt · plugin/                       (Claude with a bundle)
  artifacts/<exp>/harness/<hash>/   snapshot of each harness bundle used by the experiment
  grow/<session>/v<N>/bundle/       each grow version's bundle
  grow/<session>/v<N>/context.json  exactly what the optimizer saw
  grow/<session>/v<N>/proposal.json the raw proposal
  grow/<session>/v<N>-proposal/     the manual optimizer's working directory
  artifacts/<exp>/<run>/improve.json        improvement tasks: baseline, rounds, verdict
  artifacts/<exp>/<run>/round-<n>/          improvement tasks: each round's prompt and harness files
  artifacts/<exp>/<run>/safety.json         the run's safety report (findings, counts, sentinel decisions)
  artifacts/<exp>/<run>/sentinel.jsonl      sentinel decisions when the sentinel hook ran
  ablations/<bundle hash>/                  the full, minimal and leave-one-out bundles of `ablate run`
  logs/
```

Every file a run writes is registered as an artifact of the run (kind, path, size, SHA-256), so
the dashboard serves it at `/runs/{id}/artifacts/{kind}` and exports list it; text artifacts and
small JSON artifacts are also inlined in exports, while the JSONL stream is only listed.

`HARNESSLAB_DB_URL` points the database elsewhere (any SQLAlchemy URL); SQLite is the tested
path. Schema changes are forward-only: new tables, new nullable columns and new indexes are added
on start-up, so a database made by an older version keeps working (two commands starting at once
on an old database are fine). Each experiment records the process that runs it; an experiment
left "running" by a process that was killed is marked `interrupted` the next time a command or
the dashboard opens the database on the same machine.

## Tables

| Table | Holds |
| --- | --- |
| `experiments` | Name, suite, spec, status, repetitions, parallelism, timestamps, Harness Lab version and commit, environment and its hash, grow session id and role when part of a session. |
| `variants` | Per experiment: key, runner, model, description, recorded policies, configuration and `config_hash`, sweep factors, `harness_hash` and the bundle's file list. |
| `tasks` | Per experiment: key, name, version, `task_hash`, `spec_hash`, `prompt_hash`, base commit, repo path, tags, spec. |
| `runs` | One row per cell: status, outcome, timestamps, worktree, the reproducibility record (base commit, hashes, runner, configuration, environment, models, CLI version, session id), exit code, error, final message, runner metadata, `metrics_json` and denormalized metric columns including `llm_calls`, `improve_ratio`, `evaluator_calls`, `risky_actions`, `safety_violations` and `safe`. |
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

Variables forwarded to every child process: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`,
`LANGUAGE`, `TERM`, `TZ`, `TMPDIR`, `TMP`, `TEMP`, CA and proxy settings, `LC_*`, `XDG_*`.
Forwarded to agent processes only, by exact name (from `execution/process.py`), the provider
variables Claude Code and Codex document:

- Anthropic API and gateways: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`;
- models: `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
  `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
  `ANTHROPIC_DEFAULT_FABLE_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`;
- Amazon Bedrock: `CLAUDE_CODE_USE_BEDROCK`, `AWS_BEARER_TOKEN_BEDROCK`,
  `ANTHROPIC_BEDROCK_BASE_URL`, `CLAUDE_CODE_SKIP_BEDROCK_AUTH`,
  `ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`, `AWS_CONFIG_FILE`,
  `AWS_SHARED_CREDENTIALS_FILE`;
- Google Vertex AI: `CLAUDE_CODE_USE_VERTEX`, `ANTHROPIC_VERTEX_PROJECT_ID`,
  `ANTHROPIC_VERTEX_BASE_URL`, `CLAUDE_CODE_SKIP_VERTEX_AUTH`, `CLOUD_ML_REGION`,
  `GOOGLE_APPLICATION_CREDENTIALS` and every `VERTEX_REGION_CLAUDE_*`;
- Microsoft Foundry: `CLAUDE_CODE_USE_FOUNDRY`, `ANTHROPIC_FOUNDRY_RESOURCE`,
  `ANTHROPIC_FOUNDRY_BASE_URL`, `ANTHROPIC_FOUNDRY_API_KEY`, `ANTHROPIC_FOUNDRY_AUTH_TOKEN`,
  `CLAUDE_CODE_SKIP_FOUNDRY_AUTH`;
- Codex: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `CODEX_HOME`;

plus whatever a variant lists in `env_passthrough`. Every child also gets `NO_COLOR=1`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTHONUNBUFFERED=1`, `GIT_TERMINAL_PROMPT=0` and `HARNESSLAB=1`. Verifier and setup commands
receive no credentials. Environment variables are never logged.

# Verify the pi and Hermes Agent adapters on real runs

> **For a session picking this up:** work on branch `claude/magical-gauss-6656qh`; commit as
> Bilgin Kocak; fast-forward `main` only after green CI. Run only pi and Hermes for real, never
> Claude Code or Codex. Check `ANTHROPIC_API_KEY` by name only and never print it. Never write
> reasoning text, signatures or `<think>` spans anywhere. Report proxy 403s instead of working
> around them.

## Context

The `pi` and `hermes` runners (merged into main at 58d8acd) were built from each CLI's source and
tested only with hand-constructed streams. Hermes could not even be installed, so its `--help`
fixture is reconstructed. The user wants both adapters checked against a real model, the problems
that real streams show fixed, real streams kept as test fixtures, and the "not yet verified"
notes removed. Decisions made with the user:

- **Key:** an Anthropic Console API key in `ANTHROPIC_API_KEY`, never the Claude subscription.
  api.anthropic.com is reachable from here; openrouter.ai is blocked by the proxy.
- **Spend, in two passes:**
  - Smoke: one demo task per harness on Claude Haiku 5.5 (well under $1). Fix what it shows.
  - Demo: the three demo tasks per harness on Claude Sonnet 5.5 (roughly $3–10).
- **Scope:** pi and Hermes only. OpenCode stays marked unverified.

## What the user does first

1. Create a Console API key, preferably in a dedicated workspace with a monthly spend limit of
   about $15. Revoke it when this work is done.
2. In this environment's settings, add it as an **environment variable**:
   - Path: the cloud icon above the message box → hover the environment → settings icon → *Environment variables*.
   - Line to add: `ANTHROPIC_API_KEY=...`.
   - Do **not** add it as a network secret: the agent proxy never attaches network secrets to
     `api.anthropic.com`, and pi and Hermes must read the key from their own environment.
   - Anyone who uses the environment can read its variables.
3. Do not paste the key into the chat. I check that it is present by name only.

When the key becomes visible:
- A running session reads new values only after its VM is restored from idle.
- Otherwise a new session, following this plan as committed to the repo, sees it.

Steps 1–3 below need no key. If the key is still missing at step 4, I stop and say so.

## Spend limits

| Pass | Model | Runs | Expected | Hard stop |
| --- | --- | --- | --- | --- |
| Smoke | `claude-haiku-5-5` | `fix-month-boundary` × {pi, hermes}, plus one re-run per harness after a fix | < $1 | $1.50 total |
| Demo | `claude-sonnet-5-5` | 3 demo tasks × {pi, hermes} | $3–8 | over $2.50 for one run, or $8 total |

- Runs go one at a time. Each run is bounded by the task's 600 s timeout, and Hermes also by `max_turns: 40`.
- A recorder gate checks the running total between runs: reported cost, or the pricing-table
  estimate when the harness reports none.
- Overall ceiling: $10. If it is reached, I stop and report rather than re-run.

## Steps

`$S` = `<scratchpad>/verify`. Installs, the pricing table and scratch configuration live there and are never committed.

### 1. Preflight and installs (no spend)

- **Hand-off copy.** Commit this plan as `docs/superpowers/plans/2026-10-13-verify-pi-hermes.md`
  and push it, so a new session can follow it. `mkdocs.yml` already excludes `superpowers/` from the site.
- **No personal state.** Check that `~/.hermes`, `~/.pi` and `~/.claude/.credentials.json` do not
  exist. They do not today.
- **Install pi:** `npm install --ignore-scripts --prefix $S/pi @earendil-works/pi-coding-agent@1.1.0`, linked into `$S/bin`.
- **Install Hermes from the tag:**
  - `git clone --depth 1 --branch v2026.9.24` into `$S/hermes-src`, and assert HEAD is **f97608f**.
  - Then `UV_PYTHON=/usr/bin/python3.13 uv sync --locked` there. The fallback is `uv venv` plus `uv pip install -e .`.
  - Link it into `$S/bin`.
  - If the tag cannot drive Claude 5.5 models, which shows up at smoke, use main pinned to a recorded SHA instead and note it.
- **Real Hermes help.** Record `hermes --version` and the real `hermes chat --help`. It replaces
  `tests/fixtures/cli_help/hermes-v2026.9.24-chat.reconstructed.txt`. Diff the two, and fix any flag drift in `runners/hermes.py`.
- **Doctor.** `harnesslab doctor` with `$S/bin` first on `PATH` shows pi and hermes as `ok`.
- **Pricing table.** `$S/pricing.yaml` holds Haiku 5.5 and Sonnet 5.5 rates copied from
  Anthropic's pricing page when the run happens. The repo never hard-codes prices; see `bundled/pricing.example.yaml`.

### 2. Offline code changes, committed and pushed before any spend

**Keep Hermes off Claude Code's login** (`src/harnesslab/runners/hermes.py`).

Hermes's `resolve_anthropic_token()` falls back to `~/.claude/.credentials.json` (or the macOS
Keychain), and can write refreshed tokens back. The changes:
- `hermes_config()` adds `"auth": {"adopt_external_logins": False}`, which is Hermes's own opt-out.
- `hermes_env()` sets `CLAUDE_CONFIG_DIR` to an empty directory inside the run's state.
- Tests go in `tests/test_open_adapters.py`. A config test checks the opt-out. A fake-CLI env dump through `FAKE_CLI_ENV_OUT` checks that:
  - `CLAUDE_CONFIG_DIR` is under the state directory;
  - `ANTHROPIC_API_KEY` is forwarded;
  - `ANTHROPIC_BASE_URL` is not (this container sets it for Claude Code).
- Update the `config.yaml` block in `docs/guides/hermes.md`.

**Runner filter for the real-CLI test** (`tests/test_integration_real.py`). A comma-separated
`HARNESSLAB_INTEGRATION_RUNNERS` restricts which runners run, and the parametrize ids become the
runner names. With it, a real-CLI run cannot start Claude Code or Codex (`claude` is on `PATH` here).

**Recorder** (`scripts/record_stream.py`, tested offline with the fake CLIs in `tests/test_record_stream.py`). The `run` subcommand runs one or more tasks × one variant through
`ExperimentService.run_experiment(..., gate=..., on_run_finished=...)` with the pricing table.
- **Capture in memory only.** It wraps `PiStreamParser.feed_line` and `HermesStreamParser.feed_line` to keep raw lines in memory. It wraps `hermes.read_session` to capture the `state.db` schema and the session's numeric columns, never message or reasoning text.
- **Scrub before anything is written:**
  - thinking text and signatures become `SECRET_RECORDED_THINKING` / `SECRET_RECORDED_SIGNATURE`;
  - `<think>` spans become a marker;
  - worktree, state and home paths become `/WORKTREE`, `/STATE`, `/HOME`;
  - then `trace/redaction.py`'s `Redactor` runs over the result.
- **Gate.** It refuses to write if any reasoning value seen live, the key's value, `sk-ant-`, the
  home directory or a state path remains. It reports counts only and never prints values.
- **Replay check.** Feeding the scrubbed lines to a fresh parser must reproduce the live parser's
  usage, cost, call and turn counts, tool events, file changes and status.
- **Leak scan.** Every thinking text and signature seen live is searched for in the run's
  database rows, artifacts and logs. It prints the number of hits only, and that number must be 0.
- **Budget.** `--budget-usd` is enforced through `gate` between runs.
- **Other subcommands.** `shapes FILE` prints each record type's keys and value types (no values). `verify FILE` re-runs the gate on a fixture.

**Correct the tag commit.** "tag v2026.9.24 (commit 53bc70c)" is wrong in
`tests/fixtures/hermes/README.md` and the docstrings. The tag is f97608f; 53bc70c is main on 2026-10-12, which the adapter was read from.

**Validate and commit.** Run ruff and pytest, then commit and push.

### 3. Negative runs (no spend)

| Harness | Case | Expected |
| --- | --- | --- |
| pi | no key in the environment | *unavailable*, with a clear error |
| pi | unknown model id | *unavailable*, with a clear error |
| hermes | no key in the environment | *unavailable*, with a clear error |
| hermes | unknown model id | *unavailable*, with a clear error |

- The no-key cases run while the key is absent, or with the key withheld from the child environment.
- An unknown model is refused by the API (404) without being billed.
- In every case, no stray state is left behind.

### 4. Smoke: Claude Haiku 5.5, `fix-month-boundary`, one harness at a time

**Before running:**
- Check the key's presence by name.
- Send a free `GET /v1/models/claude-haiku-5-5`, with the key read from the environment inside the command, and print only the HTTP status.

**Variants** (in `$S/verify.yaml`; only these two ever run):
- `{runner: pi, provider: anthropic, model: claude-haiku-5-5, allow_dangerous_permissions: true}`
- `{runner: hermes, provider: anthropic, model: claude-haiku-5-5, max_turns: 40}`

**Run** with `scripts/record_stream.py run --budget-usd 1.5`.

**Check each run:**
- status and verifier result;
- reported cost against the pricing estimate;
- tokens, `llm_calls`, turns, tool events, file changes and the safety report against what
  actually happened in the worktree;
- a `shapes` diff against the constructed fixtures;
- a leak scan with 0 hits;
- no `.pi/` or `.hermes` files in the worktree, and no state directories left in the temp directory.

**Fix each difference test-first:**
- reproduce it in a constructed fixture;
- write a failing test;
- fix the parser or runner;
- commit;
- re-run only that harness once.

### 5. Demo: Claude Sonnet 5.5, three tasks × two harnesses

- Run pi's three tasks, then Hermes's three, with `--budget-usd 8`, checking and fixing as in step 4.
- Produce a results table for the user. Per task × harness: solved or not, cost, tokens, turns and duration.

### 6. Real streams as fixtures

- **Recorded fixtures.**
  - pi: `tests/fixtures/pi/recorded_haiku_fix_month_boundary.jsonl`.
  - Hermes: `tests/fixtures/hermes/recorded_haiku_fix_month_boundary.jsonl` and its `.state.json` (the session row and per-model usage that `read_session` returns).
  - Add one Sonnet demo stream per harness only if it shows record shapes the Haiku one lacks.
- **Tests:**
  - A parser replay test: every number and event matches what the live run stored.
  - A full runner replay through the fake CLI, seeding Hermes's `state.db` with `FAKE_CLI_COPY`.
  - An assertion that no `SECRET_RECORDED_*` marker reaches the events, the database or the artifacts.
- **Constructed fixtures stay.** They cover error paths no real run hit. The fixture READMEs say which files are recorded (version, model, date, scrubbing) and which are constructed.

### 7. Docs

- `docs/guides/pi.md` and `docs/guides/hermes.md`:
  - replace the "not yet verified" paragraph with what was verified (versions, models, date);
  - add what the runs taught, for example that Hermes on the tag reports no cost for 5.5 models, so use a pricing table.
- `docs/explanations/limits.md` and `docs/project/roadmap.md`: pi and Hermes are verified; OpenCode is still not.
- `tests/test_integration_real.py` docstring and the contributing guide: the runner filter and the recorder.
- `CHANGELOG.md` and `docs/project/changelog.md`, kept identical: under *Unreleased*, the credential opt-out, the fixes, and the recorded fixtures.

### 8. Verify and publish

- Run ruff (check and format), the full pytest suite, the CLI-reference check, `mkdocs build --strict` and `uv build`.
- Do one final real check of the documented path, within budget:
  `HARNESSLAB_INTEGRATION=1 HARNESSLAB_INTEGRATION_RUNNERS=pi,hermes uv run pytest tests/test_integration_real.py`
  with `HARNESSLAB_PI_MODEL` and `HARNESSLAB_HERMES_MODEL` set to Haiku 5.5.
- Commit as Bilgin Kocak with the session trailers, then push `claude/magical-gauss-6656qh`.
- Wait for CI to pass, fast-forward `main`, and wait for CI and the docs deployment.
- Remind the user to remove the variable and revoke the key.

## Rules for the real runs

- **What may run for real.** Only pi and Hermes, and only the variants above. Claude Code and Codex are never run.
- **The key.** It is never printed, written to a file, or put in a command line. The recorder gate and the leak scan check its value without showing it.
- **Reasoning.** Thinking text and signatures stay in memory. Only markers and counts are written or shown.
- **Isolation.** pi runs with `allow_dangerous_permissions` only in this throwaway container. Each run uses Harness Lab's own disposable worktree and state directory.
- **Proxy.** A proxy 403 (for example Hermes's metadata fetch from openrouter.ai) is reported, never routed around.

## Files

- **Change:**
  - `src/harnesslab/runners/hermes.py`;
  - parser and runner fixes the runs show (`trace/pi_parser.py`, `trace/hermes_parser.py`, `runners/pi.py`);
  - `tests/test_open_adapters.py`, `tests/test_integration_real.py`;
  - the fixture READMEs and the Hermes help fixture;
  - the docs listed in step 7 and both changelogs.
- **Add:**
  - `scripts/record_stream.py` and `tests/test_record_stream.py`;
  - the recorded fixtures and their replay tests;
  - `docs/superpowers/plans/2026-10-13-verify-pi-hermes.md`.

## Done when

- pi and Hermes each complete the smoke task and the three demo tasks with a stored result.
- Each such result has a verifier result, tokens, a reported or estimated cost, and tool events and file changes that match the worktree.
- The leak scans find 0 reasoning, key or path hits.
- The recorded fixtures replay to the same numbers.
- Lint, tests, strict docs and the build pass.
- CI is green on the branch and on main, and the docs are deployed.

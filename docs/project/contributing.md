# Contributing

## Set up

```bash
git clone https://github.com/bilgin-kocak/harness-lab
cd harness-lab
uv sync                       # package + dev tools + docs tools
uv run harnesslab doctor
```

## Check before you push

```bash
uv run pytest -q              # ~2 minutes; real-CLI tests are skipped unless HARNESSLAB_INTEGRATION=1
uv run ruff check src tests
uv run ruff format --check src tests
uv run mkdocs build --strict  # the docs must build without warnings
```

CI runs the same on Ubuntu and macOS with Python 3.12 and 3.13, builds the distributions, and
deploys the docs from `main`.

## Layout

```text
src/harnesslab/
  cli.py                 Typer CLI
  config.py              Settings (HARNESSLAB_HOME, default ./.harnesslab)
  core/                  models, events, metrics, pricing, ids
  runners/               HarnessRunner ABC + registry; fake, generic, codex, claude
  execution/             subprocesses, fixture snapshots, worktrees, sandboxes
  trace/                 redaction, normalization, the Codex and Claude parsers
  verification/          exit-code verifier, hidden-file injection, partial scores
  storage/               SQLAlchemy models and the repository
  experiments/           YAML loading, orchestration, aggregates, sweeps, export
  harness/               harness bundles and the leak lint
  grow/                  grow spec, service, optimizer view, report, optimizers/
  web/                   FastAPI + Jinja2 + HTMX dashboard
  bundled/               demo suite, sweep and grow templates, baseline bundle, pricing example
tests/                   unit, parser (recorded JSONL fixtures), end-to-end, CLI, web, docs, opt-in integration
docs/                    this site (GitBook and MkDocs read the same files)
scripts/                 gen_cli_reference.py
```

## Conventions

- Tests first. Every change to behaviour comes with a test that failed before it.
- Runners never touch the database or the UI; optimizers never run agents or touch the database.
- Nothing hidden is persisted: no thinking text, no secrets, no hidden test content in an
  optimizer view. The test suite scans for leaks; keep those tests green.
- New database columns must be nullable (forward-only migration).
- No new runtime dependencies without a reason in the pull request.
- Ruff, line length 100.

## Adding a runner

Subclass `HarnessRunner` (see [Python API](../reference/python-api.md)), register it, add a
fixture-driven test using the stand-in CLI in `tests/fake_clis/` and a recorded stream under
`tests/fixtures/`, and document its options in a guide page.

## Adding an optimizer

Subclass `Optimizer`, register it, and test it against the fake runner through `GrowService` so the
window, gate and leak lint are exercised.

## Documentation

Pages live in `docs/` and are listed in both `docs/SUMMARY.md` (GitBook) and the `nav` in
`mkdocs.yml` (MkDocs); `tests/test_docs.py` fails if the two disagree, if a page is unlisted, or
if an internal link is broken. The CLI reference is generated:

```bash
uv run python scripts/gen_cli_reference.py
uv run mkdocs serve
```

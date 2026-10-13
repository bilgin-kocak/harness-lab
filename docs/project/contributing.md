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
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
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
  experiments/           YAML loading, orchestration, aggregates, statistics, sweeps, ablation,
                         attempts (pass@k, best-of-k), routing, recovery, export
  harness/               harness bundles and the leak lint
  grow/                  grow spec, service, optimizer view, report, optimizers/
  improve/               improvement-task protocol: objective, evaluator, checkpoints
  safety/                risky-action rules, the run analysis, the sentinel decider
  corpus/                `suite mine`: tasks from a repository's git history
  web/                   FastAPI + Jinja2 + HTMX dashboard
  api.py                 the public extension API (also importable from `harnesslab`)
  bundled/               suites (demo, demo-improve, demo-safety, demo-discovery, demo-recovery),
                         sweep and grow templates, baseline and sentinel bundles, pricing example
tests/                   unit, parser (recorded JSONL fixtures), end-to-end, CLI, web, docs, opt-in integration
docs/                    this site (GitBook and MkDocs read the same files; the landing page,
                         index.md, is MkDocs-only and GitBook opens on start/overview.md)
  assets/                images drawn by scripts/site_assets.py
  stylesheets/           the site's CSS
overrides/main.html      the site's one template block: link previews (Open Graph)
scripts/                 gen_cli_reference.py (CLI reference), site_assets.py (site images),
                         record_stream.py (real pi and Hermes Agent runs as test fixtures)
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

The front page, `docs/index.md`, is a landing page written with Material-only syntax (cards,
buttons, icons); GitBook does not list it and opens on `start/overview.md`. Every page GitBook
shows stays plain Markdown, which `tests/test_docs.py` checks.

### Site images

The screenshots and terminal images under `docs/assets/` are drawn from the bundled demos by a
script that runs only the offline fake agents and can never start a real harness:

```bash
uv run --with "playwright==1.56.*" --with pillow python scripts/site_assets.py        # ~2 minutes
uv run --with "playwright==1.56.*" --with pillow python scripts/site_assets.py --only screenshots
uv run mkdocs build --strict
uv run --with "playwright==1.56.*" python scripts/site_assets.py verify --site site   # checks the built site
```

Playwright needs a Chromium: `uv run --with "playwright==1.56.*" playwright install chromium`, or
pass `--chromium PATH` for one you have. Regenerate only when the dashboard or the CLI output
changes: every regeneration adds about 1.5 MB to the repository's history. Keep
`experiment-matrix.png` and `social-card.jpg` under their names, because the README on PyPI and
cached link previews point at them. `tests/test_docs.py` fails when an image is unused or over the
budget (300 kB each, 2 MB in total). Set `NO_MKDOCS_2_WARNING=1` to silence Material's notice
about MkDocs 2.

### Real runs

The test suite runs every adapter on recorded or constructed streams, through a stand-in
executable. To check a real harness end to end (it uses your keys, and costs money):

```bash
HARNESSLAB_INTEGRATION=1 HARNESSLAB_INTEGRATION_RUNNERS=pi,hermes uv run pytest tests/test_integration_real.py
```

Without `HARNESSLAB_INTEGRATION_RUNNERS` every installed harness runs, Claude Code and Codex
included.

`scripts/record_stream.py` turns real pi and Hermes Agent runs into test fixtures. It runs demo
tasks with one variant from a YAML file and stops before a run once `--budget-usd` is spent:

```bash
uv run python scripts/record_stream.py run --spec verify.yaml --variant pi-haiku \
    --task fix-month-boundary --pricing pricing.yaml --budget-usd 1 --out recordings
uv run python scripts/record_stream.py verify recordings/*.jsonl
uv run python scripts/record_stream.py shapes recordings/*.jsonl
```

A run's raw stream stays in memory. Thinking text, signatures and `<think>` spans become
`SECRET_RECORDED_*` markers, and local paths become placeholders. Nothing is written if any of them
is left, if the scrubbed lines replay to different numbers than the live run, or if reasoning
turned up in the run's database, artifacts or logs. `verify` checks a recorded file again;
`shapes` lists the keys of each kind of record, to compare a real stream with a constructed one.

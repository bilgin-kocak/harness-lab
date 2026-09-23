"""Harness Lab command-line interface."""

from __future__ import annotations

import asyncio
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

import harnesslab
from harnesslab.bundled import (
    bundled_pricing_example,
    bundled_suites_dir,
    bundled_sweeps_dir,
    list_bundled_suites,
    list_bundled_sweeps,
)
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, RunStatus, TaskSpec, VariantSpec
from harnesslab.core.pricing import PricingTable, find_pricing_table
from harnesslab.experiments.aggregate import aggregate_variants, build_matrix, samples_from_rows
from harnesslab.experiments.export import export_experiment
from harnesslab.experiments.service import ExperimentOutcome, ExperimentService, RunProgress
from harnesslab.experiments.spec import (
    SpecError,
    load_run_target,
    load_suite,
    load_sweep_target,
    resolve_suite_target,
    resolve_variant_harnesses,
    resolve_variants,
    select_tasks,
)
from harnesslab.experiments.sweep import (
    BudgetGate,
    SweepReport,
    expand_sweep,
    report_for_experiment,
)
from harnesslab.runners.base import PluginError, load_plugins
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository

app = typer.Typer(
    help="Harness Lab: run the same coding task against several agent harnesses and compare verified results.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
suite_app = typer.Typer(help="Inspect and sanity-check task suites.", no_args_is_help=True)
experiment_app = typer.Typer(help="Inspect and export past experiments.", no_args_is_help=True)
sweep_app = typer.Typer(
    help="Configuration sweeps: search model x effort x toolset x compaction x action policy for the cheapest verified configuration.",
    no_args_is_help=True,
)
app.add_typer(suite_app, name="suite")
app.add_typer(experiment_app, name="experiment")
app.add_typer(sweep_app, name="sweep")

console = Console()
err_console = Console(stderr=True)


def _settings(ctx: typer.Context) -> Settings:
    settings: Settings = ctx.obj
    return settings


def _open_db(settings: Settings) -> Database:
    settings.ensure_dirs()
    db = Database(settings.resolved_database_url)
    db.create_all()
    return db


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


@app.callback()
def main(
    ctx: typer.Context,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            envvar="HARNESSLAB_HOME",
            help="Harness Lab data directory (default ./.harnesslab).",
        ),
    ] = None,
) -> None:
    if sys.platform == "win32":
        err_console.print(
            "[red]Harness Lab does not support native Windows yet (it relies on POSIX process groups and git worktrees). "
            "Please run it inside WSL.[/]"
        )
        raise typer.Exit(code=2)
    ctx.obj = Settings.from_env(home)


def _load_plugins_or_exit(modules: list[str]) -> None:
    try:
        load_plugins(modules)
    except PluginError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


@app.command()
def version() -> None:
    """Print the Harness Lab version."""
    console.print(f"harnesslab {harnesslab.__version__}")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

INIT_README = """# My Harness Lab

Scaffolded by `harnesslab init`. Everything here is yours to edit.

```bash
harnesslab doctor                                              # check python, git, codex, claude, database
harnesslab suite check suites/demo/suite.yaml                  # verifiers fail on the untouched repo, pass with the solution
harnesslab run suites/demo/suite.yaml --variants fake-reference,fake-noop
harnesslab sweep run sweeps/demo-fake.yaml                     # cheapest verified configuration (no API keys)
harnesslab serve                                               # http://127.0.0.1:8000
```

- `suites/demo/` - a copy of the bundled demo suite: tasks, hidden tests (`tasks/<id>/verify`), reference solutions.
- `sweeps/` - configuration-sweep templates (`claude-config-search.yaml` costs real API usage).
- `pricing.example.yaml` - copy to `pricing.yaml` and fill in rates to get estimated costs for harnesses that do not report cost.

Data (database, worktrees, artifacts) is written to `./.harnesslab`.
"""


@app.command()
def init(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Argument(help="Directory to scaffold (created if missing).")
    ] = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
) -> None:
    """Scaffold a lab directory with the demo suite, sweep templates and a pricing example."""
    directory = directory.expanduser().resolve()
    targets = {
        directory / "suites" / "demo": bundled_suites_dir() / "demo",
        directory / "sweeps": bundled_sweeps_dir(),
        directory / "pricing.example.yaml": bundled_pricing_example(),
        directory / "README.md": None,
    }
    existing = [str(t.relative_to(directory)) for t in targets if t.exists()]
    if existing and not force:
        err_console.print(
            f"[red]refusing to overwrite existing paths in {directory}: {', '.join(existing)} (use --force)[/]"
        )
        raise typer.Exit(code=1)
    directory.mkdir(parents=True, exist_ok=True)
    for target, source in targets.items():
        if source is None:
            target.write_text(INIT_README, encoding="utf-8")
        elif source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".harnesslab/\n__pycache__/\n", encoding="utf-8")
    console.print(f"Scaffolded Harness Lab project in [bold]{directory}[/]")
    console.print("Next: harnesslab run suites/demo/suite.yaml --variants fake-reference,fake-noop")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check python, git, codex, claude and the database."""
    from harnesslab.execution.git import git_version
    from harnesslab.runners.base import create_runner

    settings = _settings(ctx)
    table = Table(title="harnesslab doctor", show_lines=False)
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    failures = 0

    py_ok = sys.version_info >= (3, 12)
    table.add_row(
        "python",
        "[green]ok[/]" if py_ok else "[red]too old[/]",
        f"{platform.python_version()} ({sys.executable})",
    )
    failures += 0 if py_ok else 1

    gv = git_version()
    table.add_row("git", "[green]ok[/]" if gv else "[red]missing[/]", gv or "git not found on PATH")
    failures += 0 if gv else 1

    async def probe_all() -> dict[str, Any]:
        out = {}
        for name in ("codex", "claude"):
            out[name] = await create_runner(name).check_availability()
        return out

    probes = asyncio.run(probe_all())
    for name in ("codex", "claude"):
        a = probes[name]
        status = "[green]ok[/]" if a.available else "[yellow]not installed[/]"
        detail = f"{a.version or ''} {a.executable or ''}".strip() if a.available else a.detail
        table.add_row(f"{name} cli", status, detail)

    uv = shutil.which("uv")
    table.add_row(
        "uv", "[green]ok[/]" if uv else "[yellow]optional[/]", uv or "not found (optional)"
    )

    try:
        db = _open_db(settings)
        count = Repository(db, settings.home).count_experiments()
        db.dispose()
        table.add_row(
            "database", "[green]ok[/]", f"{settings.resolved_database_url} ({count} experiments)"
        )
    except Exception as exc:  # pragma: no cover - depends on filesystem state
        failures += 1
        table.add_row("database", "[red]error[/]", str(exc))

    try:
        settings.ensure_dirs()
        probe = settings.home / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        table.add_row("home dir", "[green]ok[/]", str(settings.home))
    except OSError as exc:  # pragma: no cover
        failures += 1
        table.add_row("home dir", "[red]not writable[/]", f"{settings.home}: {exc}")

    console.print(table)
    if not probes["codex"].available or not probes["claude"].available:
        console.print(
            "[dim]Missing CLIs only disable their runners; the fake runner and dashboard work without them.[/]"
        )
    raise typer.Exit(code=1 if failures else 0)


# ---------------------------------------------------------------------------
# suite
# ---------------------------------------------------------------------------


def _find_suites(target: str | None) -> list[Path]:
    if target is None:
        found = list(list_bundled_suites().values())
        local = Path("suites")
        if local.is_dir():
            found.extend(sorted(p for p in local.rglob("suite.yaml")))
        return found
    path = Path(target)
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(p for p in path.rglob("suite.yaml"))
    bundled = list_bundled_suites().get(target)
    return [bundled] if bundled else []


@suite_app.command("list")
def suite_list(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(
            help="A suite.yaml, a directory, or a bundled suite name (default: bundled suites and ./suites)."
        ),
    ] = None,
) -> None:
    """List suites, their tasks and variants."""
    suites = _find_suites(target)
    if not suites:
        names = ", ".join(list_bundled_suites()) or "none"
        err_console.print(f"no suite found for {target!r} (bundled suites: {names})")
        raise typer.Exit(code=1)
    for suite_path in suites:
        try:
            suite, tasks = load_suite(suite_path)
        except SpecError as exc:
            err_console.print(f"[red]{exc}[/]")
            continue
        bundled_tag = (
            " [dim](bundled: use the name '" + suite_path.parent.name + "')[/]"
            if suite_path.is_relative_to(bundled_suites_dir())
            else ""
        )
        console.print(f"[bold]{suite.name}[/]  [dim]{suite_path}[/]{bundled_tag}")
        if suite.description:
            console.print(f"  {suite.description.strip()}")
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("task id")
        table.add_column("name")
        table.add_column("tags")
        table.add_column("verifier")
        for task in tasks:
            verifier = task.verification.command + (
                " (+score)" if task.verification.score_command else ""
            )
            table.add_row(task.id, task.name, ", ".join(task.tags), verifier)
        console.print(table)
        if suite.variants:
            console.print(
                "  variants: " + ", ".join(f"{v.id} ({v.runner})" for v in suite.variants)
            )
        console.print()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _progress_printer(total: int):  # type: ignore[no-untyped-def]
    done = {"n": 0}

    def cb(p: RunProgress) -> None:
        if p.phase == "started":
            console.print(f"[dim]▶ {p.task_key} × {p.variant_key} (rep {p.repetition}) started[/]")
            return
        done["n"] += 1
        if p.outcome == "pass":
            mark = "[green]✔ pass[/]"
        elif p.outcome == "fail":
            mark = "[red]✘ fail[/]"
        else:
            mark = f"[yellow]⚠ {p.status}[/]"
        score = f" score={p.verified_score:.2f}" if p.verified_score is not None else ""
        secs = f" {p.wall_time_seconds:.1f}s" if p.wall_time_seconds is not None else ""
        extra = f" [dim]{p.error}[/]" if p.error and p.outcome != "pass" else ""
        console.print(
            f"[{done['n']}/{total}] {p.task_key} × {p.variant_key}: {mark}{score}{secs}{extra}"
        )

    return cb


def _print_outcome_table(
    outcome: ExperimentOutcome, tasks: list[TaskSpec], variants: list[VariantSpec]
) -> None:
    table = Table(title=f"experiment {outcome.experiment_id} — {outcome.name}")
    table.add_column("task")
    for v in variants:
        table.add_column(v.id, justify="center")
    by_cell: dict[tuple[str, str], list[Any]] = {}
    for r in outcome.runs:
        by_cell.setdefault((r.task_key, r.variant_key), []).append(r)
    for task in tasks:
        row = [task.id]
        for v in variants:
            runs = by_cell.get((task.id, v.id), [])
            if not runs:
                row.append("—")
                continue
            passed = sum(1 for r in runs if r.metrics.verified_pass)
            valid = sum(1 for r in runs if r.metrics.verified_pass is not None)
            if valid == 0:
                row.append(f"[yellow]⚠ {runs[0].status.value}[/]")
            elif passed == valid:
                row.append("[green]✔[/]" + (f" {passed}/{valid}" if valid > 1 else ""))
            elif passed == 0:
                row.append("[red]✘[/]" + (f" {passed}/{valid}" if valid > 1 else ""))
            else:
                row.append(f"[yellow]◐ {passed}/{valid}[/]")
        table.add_row(*row)
    console.print(table)


def _load_pricing(settings: Settings, explicit: Path | None) -> PricingTable | None:
    return find_pricing_table(explicit, [settings.home, Path.cwd()])


@app.command()
def run(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Argument(
            help="A suite.yaml, an experiment.yaml, or a bundled suite name such as 'demo'."
        ),
    ],
    variants: Annotated[
        str | None, typer.Option("--variants", "-v", help="Comma-separated variant ids.")
    ] = None,
    tasks: Annotated[
        str | None, typer.Option("--tasks", "-t", help="Comma-separated task ids (default: all).")
    ] = None,
    repetitions: Annotated[int | None, typer.Option("--repetitions", "-r", min=1)] = None,
    parallelism: Annotated[int | None, typer.Option("--parallelism", "-p", min=1)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Experiment name.")] = None,
    keep_worktrees: Annotated[
        bool, typer.Option("--keep-worktrees", help="Do not delete worktrees after runs.")
    ] = False,
    pricing: Annotated[
        Path | None, typer.Option("--pricing", help="pricing.yaml for cost estimates.")
    ] = None,
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", help="Python module that registers custom runners (repeatable)."),
    ] = None,
) -> None:
    """Run every task of a suite against one or more harness variants."""
    settings = _settings(ctx)
    try:
        experiment, suite, all_tasks = load_run_target(target)
        _load_plugins_or_exit(list(plugin or []) + suite.plugins + experiment.plugins)
        chosen_variants = resolve_variants(
            [v.strip() for v in variants.split(",")] if variants else None, experiment, suite
        )
        chosen_tasks = select_tasks(
            all_tasks, [t.strip() for t in tasks.split(",")] if tasks else experiment.tasks
        )
    except SpecError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    if repetitions is not None:
        experiment.repetitions = repetitions
    if parallelism is not None:
        experiment.parallelism = parallelism
    if name:
        experiment.name = name
    if keep_worktrees:
        experiment.keep_worktrees = True

    db = _open_db(settings)
    service = ExperimentService(settings, db, pricing=_load_pricing(settings, pricing))
    total = len(chosen_tasks) * len(chosen_variants) * experiment.repetitions
    console.print(
        f"[bold]{experiment.name}[/]: {len(chosen_tasks)} task(s) × {len(chosen_variants)} variant(s) × {experiment.repetitions} repetition(s) "
        f"= {total} run(s), parallelism {experiment.parallelism}, home {settings.home}"
    )
    try:
        outcome = asyncio.run(
            service.run_experiment(
                experiment,
                suite,
                chosen_tasks,
                chosen_variants,
                keep_worktrees=keep_worktrees,
                progress=_progress_printer(total),
            )
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        err_console.print("[red]interrupted[/]")
        raise typer.Exit(code=130) from None
    finally:
        db.dispose()
    _print_outcome_table(outcome, chosen_tasks, chosen_variants)
    console.print(f"experiment id: [bold]{outcome.experiment_id}[/]")
    console.print(f"inspect:  harnesslab experiment show {outcome.experiment_id}")
    console.print("dashboard: harnesslab serve  →  http://127.0.0.1:8000")
    infra = [r for r in outcome.runs if r.status not in (RunStatus.COMPLETED,)]
    if infra:
        console.print(
            f"[yellow]{len(infra)} run(s) did not complete normally (see errors above).[/]"
        )


@suite_app.command("check")
def suite_check(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="suite.yaml to validate, or a bundled suite name.")],
) -> None:
    """Sanity-check verifiers: reference solutions must pass, the untouched repo must fail."""
    settings = _settings(ctx)
    try:
        suite_path = resolve_suite_target(target)
        suite, tasks = load_suite(suite_path)
    except SpecError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    experiment = ExperimentSpec(
        name=f"suite-check:{suite.name}",
        suite=str(suite_path),
        parallelism=2,
        source_path=suite_path.resolve(),
    )
    variants = [
        VariantSpec(
            id="check-reference", runner="fake", behavior="solve", description="reference solution"
        ),
        VariantSpec(
            id="check-noop", runner="fake", behavior="noop", description="untouched repository"
        ),
    ]
    db = _open_db(settings)
    service = ExperimentService(settings, db)
    try:
        outcome = asyncio.run(service.run_experiment(experiment, suite, tasks, variants))
    finally:
        db.dispose()
    problems = 0
    table = Table(title=f"suite check: {suite.name}")
    table.add_column("task")
    table.add_column("reference solution")
    table.add_column("untouched repo")
    table.add_column("verdict")
    for task in tasks:
        ref = next(
            (
                r
                for r in outcome.runs
                if r.task_key == task.id and r.variant_key == "check-reference"
            ),
            None,
        )
        noop = next(
            (r for r in outcome.runs if r.task_key == task.id and r.variant_key == "check-noop"),
            None,
        )
        ref_ok = bool(ref and ref.metrics.verified_pass)
        noop_fails = bool(noop and noop.metrics.verified_pass is False)
        has_ref = task.reference_solution is not None and task.reference_solution.overlay
        verdict = "[green]ok[/]"
        if not noop_fails:
            verdict = "[red]verifier passes without changes[/]"
            problems += 1
        elif has_ref and not ref_ok:
            verdict = "[red]reference solution fails[/]"
            problems += 1
        elif not has_ref:
            verdict = "[yellow]no reference solution[/]"
        table.add_row(
            task.id,
            ("pass" if ref_ok else "fail") if has_ref else "n/a",
            "fail" if noop_fails else "PASS",
            verdict,
        )
    console.print(table)
    raise typer.Exit(code=1 if problems else 0)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    """Start the local dashboard."""
    import uvicorn

    from harnesslab.web.app import create_app

    settings = _settings(ctx)
    console.print(f"Harness Lab dashboard on http://{host}:{port}  (data: {settings.home})")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# experiment
# ---------------------------------------------------------------------------


@experiment_app.command("list")
def experiment_list(ctx: typer.Context) -> None:
    """List past experiments."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        rows = Repository(db, settings.home).list_experiments()
    finally:
        db.dispose()
    table = Table()
    for col in (
        "id",
        "name",
        "created",
        "suite",
        "variants",
        "runs",
        "passed",
        "best score",
        "status",
    ):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["id"],
            r["name"],
            r["created_at"].strftime("%Y-%m-%d %H:%M"),
            r["suite_name"],
            ", ".join(r["variants"]),
            str(r["runs"]),
            str(r["passed"]),
            _fmt(r["best_score"]),
            r["status"],
        )
    console.print(table)


@experiment_app.command("show")
def experiment_show(ctx: typer.Context, experiment_id: Annotated[str, typer.Argument()]) -> None:
    """Print a terminal summary of an experiment."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        repo = Repository(db, settings.home)
        exp = repo.find_experiment(experiment_id)
        if exp is None:
            err_console.print(f"[red]experiment not found: {experiment_id}[/]")
            raise typer.Exit(code=1)
        tasks_by_id = {t.id: t for t in exp.tasks}
        variants_by_id = {v.id: v for v in exp.variants}
        samples = samples_from_rows(exp.runs, tasks_by_id, variants_by_id)
    finally:
        db.dispose()
    variant_keys = [v.variant_key for v in exp.variants]
    task_keys = [t.task_key for t in exp.tasks]
    console.print(
        f"[bold]{exp.name}[/]  id={exp.id}  suite={exp.suite_name}  status={exp.status}  created={exp.created_at:%Y-%m-%d %H:%M}"
    )
    console.print(
        f"harnesslab {exp.harnesslab_version} commit={exp.harnesslab_commit or '—'}  env={exp.environment_hash}"
    )

    matrix = build_matrix(samples, task_keys, variant_keys)
    mt = Table(title="task × variant (verified)")
    mt.add_column("task")
    for vk in variant_keys:
        mt.add_column(vk, justify="center")
    for tk in task_keys:
        row = [tk]
        for vk in variant_keys:
            cell = matrix[tk][vk]
            if cell.state == "pass":
                row.append(
                    "[green]✔[/]" + (f" {cell.n_passed}/{cell.n_valid}" if cell.n > 1 else "")
                )
            elif cell.state == "fail":
                row.append("[red]✘[/]" + (f" {cell.n_passed}/{cell.n_valid}" if cell.n > 1 else ""))
            elif cell.state == "mixed":
                row.append(f"[yellow]◐ {cell.n_passed}/{cell.n_valid}[/]")
            elif cell.state == "error":
                row.append(f"[yellow]⚠ {cell.runs[0].status}[/]")
            else:
                row.append("—")
        mt.add_row(*row)
    console.print(mt)

    aggs = aggregate_variants(samples, variant_keys)
    at = Table(title="variant aggregates")
    at.add_column("metric")
    for vk in variant_keys:
        at.add_column(vk, justify="right")

    def stat_row(label: str, getter, digits: int = 2) -> None:  # type: ignore[no-untyped-def]
        at.add_row(label, *[_fmt(getter(aggs[vk]), digits) for vk in variant_keys])

    at.add_row(
        "runs (valid/total)", *[f"{aggs[vk].n_valid}/{aggs[vk].n_total}" for vk in variant_keys]
    )
    at.add_row(
        "pass rate",
        *[
            (
                f"{aggs[vk].success_rate * 100:.0f}% ({aggs[vk].n_passed}/{aggs[vk].n_valid})"
                if aggs[vk].success_rate is not None
                else "—"
            )
            for vk in variant_keys
        ],
    )
    stat_row("mean score", lambda a: a.score.mean)
    stat_row("score std", lambda a: a.score.std)
    stat_row("median wall time (s)", lambda a: a.wall_time_seconds.median)
    stat_row("median input tokens", lambda a: a.input_tokens.median, 0)
    stat_row("median output tokens", lambda a: a.output_tokens.median, 0)
    stat_row("median tool calls", lambda a: a.tool_calls.median, 0)
    stat_row("median files changed", lambda a: a.files_changed.median, 0)
    stat_row("median reported cost (USD)", lambda a: a.reported_cost_usd.median, 4)
    stat_row("median estimated cost (USD)", lambda a: a.estimated_cost_usd.median, 4)
    at.add_row("infra failures", *[str(aggs[vk].n_infra_failures) for vk in variant_keys])
    console.print(at)

    rt = Table(title="runs")
    for col in (
        "run id",
        "task",
        "variant",
        "rep",
        "status",
        "outcome",
        "score",
        "time (s)",
        "tokens in/out",
        "tools",
        "files",
    ):
        rt.add_column(col)
    for s in samples:
        rt.add_row(
            s.run_id,
            s.task_key,
            s.variant_key,
            str(s.repetition),
            s.status,
            s.outcome,
            _fmt(s.verified_score),
            _fmt(s.wall_time_seconds, 1),
            f"{_fmt(s.input_tokens)}/{_fmt(s.output_tokens)}",
            _fmt(s.tool_calls),
            _fmt(s.files_changed),
        )
    console.print(rt)


@experiment_app.command("export")
def experiment_export(
    ctx: typer.Context,
    experiment_id: Annotated[str, typer.Argument()],
    events: Annotated[
        bool, typer.Option("--events/--no-events", help="Include normalized events.")
    ] = True,
    artifacts: Annotated[
        bool,
        typer.Option(
            "--artifacts/--no-artifacts", help="Inline text artifacts (diffs, verifier output)."
        ),
    ] = True,
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to a file instead of stdout.")
    ] = None,
) -> None:
    """Export an experiment (spec, runs, traces, verdicts, aggregates) as JSON."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        repo = Repository(db, settings.home)
        exp = repo.find_experiment(experiment_id)
        if exp is None:
            err_console.print(f"[red]experiment not found: {experiment_id}[/]")
            raise typer.Exit(code=1)
        data = export_experiment(repo, exp.id, include_events=events, include_artifacts=artifacts)
    finally:
        db.dispose()
    text = json.dumps(data, indent=2, default=str)
    if output:
        output.write_text(text, encoding="utf-8")
        console.print(f"wrote {output}")
    else:
        sys.stdout.write(text + "\n")


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def _fmt_objective(value: float | None, kind: str) -> str:
    if value is None:
        return "—"
    if kind.endswith("cost_usd"):
        return f"${value:,.4f}"
    if kind == "wall_time_seconds":
        return f"{value:,.1f}s"
    if kind == "llm_calls":
        return f"{value:,.0f} calls"
    return f"{value:,.0f}"


def _print_sweep_report(report: SweepReport) -> None:
    kind = report.objective_kind
    console.print(
        f"[bold]sweep {report.name}[/]: {report.n_configs} configuration(s), {report.n_runs} run(s)"
        + (f", {report.n_skipped} skipped by budget" if report.n_skipped else "")
        + f" · objective: minimize [bold]{kind}[/] subject to pass rate ≥ {report.min_pass_rate:.0%}"
    )
    for note in report.notes:
        console.print(f"[dim]note: {note}[/]")
    for w in report.workloads:
        console.print()
        title = f"workload [bold]{w.workload}[/] ({w.kind}: {', '.join(w.task_keys)})"
        if w.recommended:
            r = w.recommended
            console.print(
                f"{title}\n  [green]cheapest verified configuration:[/] [bold]{r.variant_key}[/]"
                f"  pass {r.pass_rate:.0%} ({r.n_passed}/{r.n_valid}) · {kind} {_fmt_objective(r.objective, kind)}"
                f" · tokens {_fmt(r.median_tokens, 0)} · wall {_fmt(r.median_wall_time, 1)}s"
            )
            if w.runner_up:
                u = w.runner_up
                console.print(
                    f"  runner-up: {u.variant_key}  {kind} {_fmt_objective(u.objective, kind)}"
                )
            if w.holdout:
                h = w.holdout
                rate = f"{h.pass_rate:.0%}" if h.pass_rate is not None else "—"
                console.print(
                    f"  holdout ({', '.join(h.task_keys)}): pass {rate} over {h.n_valid} run(s) · {kind} {_fmt_objective(h.objective, kind)}"
                )
        else:
            console.print(f"{title}\n  [red]no configuration met the requirement[/]")
            if w.best_effort:
                b = w.best_effort
                console.print(
                    f"  best effort: {b.variant_key}  pass {b.pass_rate:.0%} · {kind} {_fmt_objective(b.objective, kind)}"
                )
        table = Table(show_header=True, box=None, padding=(0, 1))
        for col in (
            "",
            "configuration",
            "pass",
            "valid",
            kind,
            "tokens",
            "wall (s)",
            "tools",
            "note",
        ):
            table.add_column(
                col,
                justify="right"
                if col in (kind, "tokens", "wall (s)", "tools", "pass", "valid")
                else "left",
            )
        for c in w.configs:
            mark = (
                "[green]✔[/]"
                if c.eligible
                else ("[yellow]◆[/]" if c.variant_key in w.pareto else "")
            )
            table.add_row(
                mark,
                c.variant_key,
                f"{c.pass_rate:.0%}" if c.pass_rate is not None else "—",
                f"{c.n_valid}/{c.n_total}",
                _fmt_objective(c.objective, kind),
                _fmt(c.median_tokens, 0),
                _fmt(c.median_wall_time, 1),
                _fmt(c.median_tool_calls, 0),
                c.reason or "",
            )
        console.print(table)
    if report.factor_effects:
        console.print()
        ft = Table(
            title="factor effects (marginal means over configurations)", box=None, padding=(0, 1)
        )
        for col in ("factor", "level", "configs", "mean pass rate", f"median {kind}"):
            ft.add_column(
                col,
                justify="right"
                if col in ("configs", "mean pass rate", f"median {kind}")
                else "left",
            )
        for e in report.factor_effects:
            ft.add_row(
                e.factor,
                e.level,
                str(e.n_configs),
                f"{e.mean_pass_rate:.0%}" if e.mean_pass_rate is not None else "—",
                _fmt_objective(e.median_objective, kind),
            )
        console.print(ft)
    console.print("[dim]✔ eligible · ◆ on the pass-rate/cost Pareto front[/]")


@sweep_app.command("list")
def sweep_list() -> None:
    """List bundled sweep templates and ./sweeps/*.yaml."""
    for name, path in list_bundled_sweeps().items():
        console.print(f"[bold]{name}[/]  [dim]{path}[/]  (bundled)")
    local = Path("sweeps")
    if local.is_dir():
        for path in sorted(local.glob("*.yaml")):
            console.print(f"[bold]{path.stem}[/]  [dim]{path}[/]")


@sweep_app.command("run")
def sweep_run(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="A sweep.yaml or a bundled sweep name (e.g. demo-fake).")
    ],
    repetitions: Annotated[int | None, typer.Option("--repetitions", "-r", min=1)] = None,
    parallelism: Annotated[int | None, typer.Option("--parallelism", "-p", min=1)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Experiment name.")] = None,
    keep_worktrees: Annotated[bool, typer.Option("--keep-worktrees")] = False,
    pricing: Annotated[
        Path | None, typer.Option("--pricing", help="pricing.yaml for cost estimates.")
    ] = None,
    plugin: Annotated[
        list[str] | None, typer.Option("--plugin", help="Python module registering custom runners.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the expanded configurations and exit.")
    ] = False,
) -> None:
    """Run every configuration of a sweep and report the cheapest verified one per workload."""
    settings = _settings(ctx)
    try:
        spec, suite, tasks = load_sweep_target(target)
        _load_plugins_or_exit(list(plugin or []) + suite.plugins + spec.plugins)
        variants = expand_sweep(spec)
        resolve_variant_harnesses(variants, spec.base_dir)
    except SpecError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    if repetitions is not None:
        spec.repetitions = repetitions
    if parallelism is not None:
        spec.parallelism = parallelism
    total = len(variants) * len(tasks) * spec.repetitions
    console.print(
        f"[bold]{name or spec.name}[/]: {len(variants)} configuration(s) × {len(tasks)} task(s) × {spec.repetitions} repetition(s) = {total} run(s)"
        + (
            f" · budget max_runs={spec.budget.max_runs} max_cost_usd={spec.budget.max_cost_usd}"
            if spec.budget
            else ""
        )
    )
    if dry_run:
        for v in variants:
            console.print(f"  {v.id}")
        return
    if spec.budget and spec.budget.max_runs is not None and total > spec.budget.max_runs:
        console.print(
            f"[yellow]{total - spec.budget.max_runs} run(s) will be skipped by max_runs; add `sample:` to subset the grid instead.[/]"
        )
    experiment = ExperimentSpec(
        name=name or spec.name,
        suite=spec.suite,
        description=spec.description,
        repetitions=spec.repetitions,
        parallelism=spec.parallelism,
        variants=variants,
        keep_worktrees=keep_worktrees or spec.keep_worktrees,
        plugins=spec.plugins,
        sweep=spec.model_dump(mode="json"),
        source_path=spec.source_path,
    )
    gate = BudgetGate(spec.budget.max_runs, spec.budget.max_cost_usd) if spec.budget else None
    db = _open_db(settings)
    service = ExperimentService(settings, db, pricing=_load_pricing(settings, pricing))
    try:
        outcome = asyncio.run(
            service.run_experiment(
                experiment,
                suite,
                tasks,
                variants,
                keep_worktrees=keep_worktrees,
                progress=_progress_printer(total),
                gate=gate.check if gate else None,
                on_run_finished=(
                    lambda o: gate.on_finished(
                        o.metrics.reported_cost_usd, o.metrics.estimated_cost_usd
                    )
                )
                if gate
                else None,
            )
        )
        exp = service.repo.get_experiment(outcome.experiment_id)
        report = report_for_experiment(exp)
    finally:
        db.dispose()
    console.print()
    if report is not None:
        _print_sweep_report(report)
    console.print(
        f"experiment id: [bold]{outcome.experiment_id}[/]  ·  harnesslab sweep report {outcome.experiment_id}  ·  harnesslab serve"
    )


@sweep_app.command("report")
def sweep_report(ctx: typer.Context, experiment_id: Annotated[str, typer.Argument()]) -> None:
    """Recompute the recommendation of a past sweep from the database."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        exp = Repository(db, settings.home).find_experiment(experiment_id)
        if exp is None:
            err_console.print(f"[red]experiment not found: {experiment_id}[/]")
            raise typer.Exit(code=1)
        report = report_for_experiment(exp)
    finally:
        db.dispose()
    if report is None:
        err_console.print(f"[red]experiment {exp.id} is not a sweep (no factor grid recorded)[/]")
        raise typer.Exit(code=1)
    _print_sweep_report(report)


if __name__ == "__main__":  # pragma: no cover
    app()

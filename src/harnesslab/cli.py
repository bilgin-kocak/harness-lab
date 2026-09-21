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
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, RunStatus, SuiteSpec, TaskSpec, VariantSpec
from harnesslab.core.pricing import PricingTable, find_pricing_table
from harnesslab.experiments.aggregate import aggregate_variants, build_matrix, samples_from_rows
from harnesslab.experiments.export import export_experiment
from harnesslab.experiments.service import ExperimentOutcome, ExperimentService, RunProgress
from harnesslab.experiments.spec import (
    SpecError,
    load_run_target,
    load_suite,
    resolve_variants,
    select_tasks,
)
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository

app = typer.Typer(
    help="Harness Lab: run the same coding task against several agent harnesses and compare verified results.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
suite_app = typer.Typer(help="Inspect and sanity-check task suites.", no_args_is_help=True)
experiment_app = typer.Typer(help="Inspect and export past experiments.", no_args_is_help=True)
app.add_typer(suite_app, name="suite")
app.add_typer(experiment_app, name="experiment")

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
    home: Annotated[Path | None, typer.Option("--home", envvar="HARNESSLAB_HOME", help="Harness Lab data directory (default ./.harnesslab).")] = None,
) -> None:
    ctx.obj = Settings.from_env(home)


@app.command()
def version() -> None:
    """Print the Harness Lab version."""
    console.print(f"harnesslab {harnesslab.__version__}")


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
    table.add_row("python", "[green]ok[/]" if py_ok else "[red]too old[/]", f"{platform.python_version()} ({sys.executable})")
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
    table.add_row("uv", "[green]ok[/]" if uv else "[yellow]optional[/]", uv or "not found (optional)")

    try:
        db = _open_db(settings)
        count = Repository(db, settings.home).count_experiments()
        db.dispose()
        table.add_row("database", "[green]ok[/]", f"{settings.resolved_database_url} ({count} experiments)")
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
        console.print("[dim]Missing CLIs only disable their runners; the fake runner and dashboard work without them.[/]")
    raise typer.Exit(code=1 if failures else 0)


# ---------------------------------------------------------------------------
# suite
# ---------------------------------------------------------------------------


def _find_suites(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("suite.yaml"))


@suite_app.command("list")
def suite_list(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="A suite.yaml or a directory to search.")] = Path("suites"),
) -> None:
    """List suites, their tasks and variants."""
    suites = _find_suites(path)
    if not suites:
        err_console.print(f"no suite.yaml found under {path}")
        raise typer.Exit(code=1)
    for suite_path in suites:
        try:
            suite, tasks = load_suite(suite_path)
        except SpecError as exc:
            err_console.print(f"[red]{exc}[/]")
            continue
        console.print(f"[bold]{suite.name}[/]  [dim]{suite_path}[/]")
        if suite.description:
            console.print(f"  {suite.description.strip()}")
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("task id")
        table.add_column("name")
        table.add_column("tags")
        table.add_column("verifier")
        for task in tasks:
            verifier = task.verification.command + (" (+score)" if task.verification.score_command else "")
            table.add_row(task.id, task.name, ", ".join(task.tags), verifier)
        console.print(table)
        if suite.variants:
            console.print("  variants: " + ", ".join(f"{v.id} ({v.runner})" for v in suite.variants))
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
        console.print(f"[{done['n']}/{total}] {p.task_key} × {p.variant_key}: {mark}{score}{secs}{extra}")

    return cb


def _print_outcome_table(outcome: ExperimentOutcome, tasks: list[TaskSpec], variants: list[VariantSpec]) -> None:
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
    target: Annotated[Path, typer.Argument(help="A suite.yaml or an experiment.yaml.")],
    variants: Annotated[str | None, typer.Option("--variants", "-v", help="Comma-separated variant ids.")] = None,
    tasks: Annotated[str | None, typer.Option("--tasks", "-t", help="Comma-separated task ids (default: all).")] = None,
    repetitions: Annotated[int | None, typer.Option("--repetitions", "-r", min=1)] = None,
    parallelism: Annotated[int | None, typer.Option("--parallelism", "-p", min=1)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Experiment name.")] = None,
    keep_worktrees: Annotated[bool, typer.Option("--keep-worktrees", help="Do not delete worktrees after runs.")] = False,
    pricing: Annotated[Path | None, typer.Option("--pricing", help="pricing.yaml for cost estimates.")] = None,
) -> None:
    """Run every task of a suite against one or more harness variants."""
    settings = _settings(ctx)
    try:
        experiment, suite, all_tasks = load_run_target(target)
        chosen_variants = resolve_variants([v.strip() for v in variants.split(",")] if variants else None, experiment, suite)
        chosen_tasks = select_tasks(all_tasks, [t.strip() for t in tasks.split(",")] if tasks else experiment.tasks)
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
        console.print(f"[yellow]{len(infra)} run(s) did not complete normally (see errors above).[/]")


@suite_app.command("check")
def suite_check(
    ctx: typer.Context,
    suite_path: Annotated[Path, typer.Argument(help="suite.yaml to validate.")],
) -> None:
    """Sanity-check verifiers: reference solutions must pass, the untouched repo must fail."""
    settings = _settings(ctx)
    try:
        suite, tasks = load_suite(suite_path)
    except SpecError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    experiment = ExperimentSpec(name=f"suite-check:{suite.name}", suite=str(suite_path), parallelism=2, source_path=suite_path.resolve())
    variants = [
        VariantSpec(id="check-reference", runner="fake", behavior="solve", description="reference solution"),
        VariantSpec(id="check-noop", runner="fake", behavior="noop", description="untouched repository"),
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
        ref = next((r for r in outcome.runs if r.task_key == task.id and r.variant_key == "check-reference"), None)
        noop = next((r for r in outcome.runs if r.task_key == task.id and r.variant_key == "check-noop"), None)
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
    for col in ("id", "name", "created", "suite", "variants", "runs", "passed", "best score", "status"):
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
    console.print(f"[bold]{exp.name}[/]  id={exp.id}  suite={exp.suite_name}  status={exp.status}  created={exp.created_at:%Y-%m-%d %H:%M}")
    console.print(f"harnesslab {exp.harnesslab_version} commit={exp.harnesslab_commit or '—'}  env={exp.environment_hash}")

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
                row.append("[green]✔[/]" + (f" {cell.n_passed}/{cell.n_valid}" if cell.n > 1 else ""))
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

    at.add_row("runs (valid/total)", *[f"{aggs[vk].n_valid}/{aggs[vk].n_total}" for vk in variant_keys])
    at.add_row("pass rate", *[(f"{aggs[vk].success_rate * 100:.0f}% ({aggs[vk].n_passed}/{aggs[vk].n_valid})" if aggs[vk].success_rate is not None else "—") for vk in variant_keys])
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
    for col in ("run id", "task", "variant", "rep", "status", "outcome", "score", "time (s)", "tokens in/out", "tools", "files"):
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
    events: Annotated[bool, typer.Option("--events/--no-events", help="Include normalized events.")] = True,
    artifacts: Annotated[bool, typer.Option("--artifacts/--no-artifacts", help="Inline text artifacts (diffs, verifier output).")] = True,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write to a file instead of stdout.")] = None,
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


if __name__ == "__main__":  # pragma: no cover
    app()

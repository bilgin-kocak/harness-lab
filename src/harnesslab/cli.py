"""Harness Lab command-line interface."""

from __future__ import annotations

import asyncio
import json
import platform
import shlex
import shutil
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

import harnesslab
from harnesslab.bundled import (
    bundled_grow_dir,
    bundled_harnesses_dir,
    bundled_pricing_example,
    bundled_suites_dir,
    bundled_sweeps_dir,
    list_bundled_grows,
    list_bundled_suites,
    list_bundled_sweeps,
)
from harnesslab.config import Settings
from harnesslab.core.models import ExperimentSpec, RunStatus, TaskSpec, VariantSpec
from harnesslab.core.pricing import PricingTable, find_pricing_table
from harnesslab.corpus.mine import (
    DEFAULT_TEST_COMMAND,
    CommitRecord,
    MineOptions,
    MiningError,
    mine_repository,
)
from harnesslab.experiments.ablation import AblationReport, plan_ablation
from harnesslab.experiments.ablation import report_for_experiment as ablation_report_for
from harnesslab.experiments.aggregate import aggregate_variants, build_matrix, samples_from_rows
from harnesslab.experiments.export import export_experiment
from harnesslab.experiments.routing import RoutingGap, routing_gap
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
from harnesslab.experiments.stats import METRIC_LABELS, PairedComparison, paired_comparison
from harnesslab.experiments.sweep import (
    BudgetGate,
    SweepReport,
    expand_sweep,
    report_for_experiment,
)
from harnesslab.grow.optimizers.base import (
    EditConstraintsSpec,
    FailureCase,
    FailureMetrics,
    OptimizerContext,
    load_optimizer_plugins,
)
from harnesslab.grow.report import GrowReport, lineage_json, report_for_session
from harnesslab.grow.service import GrowError, GrowProgress, GrowService
from harnesslab.grow.spec import load_grow_target
from harnesslab.grow.view import allowed_paths
from harnesslab.harness.bundle import BundleError, HarnessBundle
from harnesslab.harness.lint import EditConstraints, SuiteSecrets, lint_candidate
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
grow_app = typer.Typer(
    help="Growing Harness: grow a harness bundle from failures with a held-out gate.",
    no_args_is_help=True,
)
harness_app = typer.Typer(help="Inspect and validate harness bundles.", no_args_is_help=True)
ablate_app = typer.Typer(
    help="Component ablation: test every part of a harness bundle against its own absence.",
    no_args_is_help=True,
)
app.add_typer(suite_app, name="suite")
app.add_typer(experiment_app, name="experiment")
app.add_typer(sweep_app, name="sweep")
app.add_typer(grow_app, name="grow")
app.add_typer(harness_app, name="harness")
app.add_typer(ablate_app, name="ablate")

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


def _signed(value: float, digits: int) -> str:
    return f"{value:+.{digits}f}"


class MetricChoice(StrEnum):
    """The metric a paired verdict is about (see ``harnesslab.experiments.stats``)."""

    pass_rate = "pass_rate"
    score = "score"
    improve_ratio = "improve_ratio"


METRIC_HELP = (
    "Metric the verdict, wins/losses and sign test are about: pass_rate, score (verified_score; "
    "recommended for improvement tasks) or improve_ratio (tasks where both sides have a ratio)."
)


def _print_paired(p: PairedComparison, title: str | None = None) -> None:
    """One evidence block: verdict, task counts, sign test and bootstrap intervals."""
    colour = {"better": "green", "worse": "red"}.get(p.verdict, "yellow")
    sign = f", sign test p = {p.sign_test_p:.3f}" if p.sign_test_p is not None else ""
    counted = (
        str(p.n_metric_tasks)
        if p.n_metric_tasks == p.n_tasks
        else f"{p.n_metric_tasks} of {p.n_tasks}"
    )
    console.print(
        f"{title or f'{p.b} vs {p.a}'}: [{colour}]{p.verdict}[/] on {p.metric_label} over "
        f"{counted} paired task(s) (wins {p.wins}, losses {p.losses}, ties {p.ties}{sign})"
    )
    rows = [
        ("pass rate (pts)", p.pass_rate_diff, 100.0, 1),
        ("score", p.score_diff, 1.0, 2),
        (p.cost_kind, p.cost_diff, 1.0, 4),
        ("llm_calls", p.llm_calls_diff, 1.0, 2),
        ("improvement ratio", p.improve_ratio_diff, 1.0, 2),
    ]
    for label, iv, scale, digits in rows:
        if iv is None:
            continue
        console.print(
            f"  {label}: {_signed(iv.estimate * scale, digits)} "
            f"[{_signed(iv.low * scale, digits)}, {_signed(iv.high * scale, digits)}] "
            f"{iv.level:.0%} interval, P(>0) {iv.p_positive:.0%}"
        )
    if not p.enough_tasks:
        with_metric = "" if p.metric == "pass_rate" else f" with {p.metric_label} on both sides"
        console.print(
            f"  [dim]fewer than {p.min_tasks} paired tasks{with_metric}: no verdict "
            "(repetitions do not count as tasks)[/]"
        )


def _print_routing(gap: RoutingGap | None, indent: str = "") -> None:
    """One line: best single variant vs choosing per task, in sample and held out."""
    if gap is not None:
        console.print(f"{indent}per-task selection: {escape(gap.summary())}")


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
        load_optimizer_plugins(modules)
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
harnesslab grow run grow/demo-fake.yaml                        # grow a harness from failures (no API keys)
harnesslab serve                                               # http://127.0.0.1:8000
```

- `suites/demo/` - a copy of the bundled demo suite: tasks, hidden tests (`tasks/<id>/verify`), reference solutions.
- `sweeps/` - configuration-sweep templates (`claude-config-search.yaml` costs real API usage).
- `harnesses/baseline/` - a starting harness bundle (system prompt, skills, hooks, fake.yaml).
- `grow/` - Growing Harness session templates (`claude-grow.yaml` costs real API usage).
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
        directory / "suites" / "demo-improve": bundled_suites_dir() / "demo-improve",
        directory / "suites" / "demo-safety": bundled_suites_dir() / "demo-safety",
        directory / "harnesses" / "baseline": bundled_harnesses_dir() / "baseline",
        directory / "harnesses" / "sentinel": bundled_harnesses_dir() / "sentinel",
        directory / "grow": bundled_grow_dir(),
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


def _print_axes(outcome: ExperimentOutcome, variants: list[VariantSpec]) -> None:
    """One line each for safety and improvement, when the runs have them."""
    safety, improvement = [], []
    for v in variants:
        runs = [r for r in outcome.runs if r.variant_key == v.id]
        judged = [r for r in runs if r.metrics.safe is not None]
        if judged:
            safe = sum(1 for r in judged if r.metrics.safe)
            violations = sum(r.metrics.safety_violations or 0 for r in judged)
            colour = "green" if safe == len(judged) else "red"
            safety.append(
                f"{v.id} [{colour}]{safe}/{len(judged)} safe[/]"
                + (f" ({violations} violation(s))" if violations else "")
            )
        ratios = sorted(
            r.metrics.improve_ratio for r in runs if r.metrics.improve_ratio is not None
        )
        if ratios:
            improvement.append(f"{v.id} {ratios[len(ratios) // 2]:.1f}x")
    if any("violation" in s for s in safety):
        console.print("safety: " + " · ".join(safety))
    if improvement:
        console.print("median improvement over baseline: " + " · ".join(improvement))


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
    _print_axes(outcome, chosen_variants)
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
        has_ref = task.reference_solution is not None and bool(
            task.reference_solution.overlay or task.reference_solution.improve_overlays
        )
        verdict = "[green]ok[/]"
        if task.improve is not None and noop is not None and noop.metrics.verified_pass is None:
            verdict = "[red]baseline fails the correctness gate[/]"
            problems += 1
        elif not noop_fails:
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


@suite_app.command("mine")
def suite_mine(
    repo: Annotated[Path, typer.Argument(help="A local git repository to mine (only read).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Directory to write the suite to.")],
    rev: Annotated[
        str, typer.Option("--rev", help="Mine commits reachable from this ref.")
    ] = "HEAD",
    max_commits: Annotated[
        int, typer.Option("--max-commits", min=1, help="Newest non-merge commits to scan.")
    ] = 200,
    max_tasks: Annotated[
        int | None, typer.Option("--max-tasks", min=1, help="Stop after this many kept tasks.")
    ] = None,
    test_command: Annotated[
        str,
        typer.Option(
            "--test-command",
            help="Verifier command; {tests} becomes the commit's test files (shell-quoted).",
        ),
    ] = DEFAULT_TEST_COMMAND,
    test_glob: Annotated[
        list[str] | None,
        typer.Option(
            "--test-glob", help="Glob marking test files (repeatable; replaces defaults)."
        ),
    ] = None,
    ignore_glob: Annotated[
        list[str] | None,
        typer.Option(
            "--ignore-glob",
            help="Glob for files that are neither tests nor source, e.g. docs (repeatable; replaces defaults).",
        ),
    ] = None,
    max_files: Annotated[
        int, typer.Option("--max-files", min=1, help="Skip commits changing more source files.")
    ] = 6,
    max_lines: Annotated[
        int, typer.Option("--max-lines", min=1, help="Skip commits changing more source lines.")
    ] = 400,
    setup: Annotated[
        list[str] | None,
        typer.Option("--setup", help="Setup command run before the tests (repeatable)."),
    ] = None,
    prompt_template: Annotated[
        Path | None,
        typer.Option("--prompt-template", help="Text file with a {message} placeholder."),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="Keep only commits whose tests fail at the parent and pass at the commit.",
        ),
    ] = True,
    timeout: Annotated[
        int, typer.Option("--timeout", min=1, help="Seconds per setup or test command.")
    ] = 300,
    parallelism: Annotated[int, typer.Option("--parallelism", "-p", min=1)] = 4,
    name: Annotated[str | None, typer.Option("--name", help="Suite name.")] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a previous suite in --out.")
    ] = False,
) -> None:
    """Mine verifier-backed tasks from a repository's git history.

    Every commit that changes source code and its tests becomes a candidate task: start at the
    parent, the commit's tests are the hidden verifier, its source change is the reference
    solution, its message is the prompt.  Candidates are kept when the tests fail at the parent
    and pass at the commit.
    """
    options = MineOptions(
        rev=rev,
        max_commits=max_commits,
        max_tasks=max_tasks,
        test_command=test_command,
        max_files=max_files,
        max_lines=max_lines,
        setup=list(setup or []),
        validate_tasks=validate,
        timeout_seconds=timeout,
        parallelism=parallelism,
        suite_name=name,
    )
    if test_glob:
        options.test_globs = list(test_glob)
    if ignore_glob:
        options.ignore_globs = list(ignore_glob)
    if prompt_template is not None:
        template = prompt_template.read_text(encoding="utf-8")
        if "{message}" not in template:
            err_console.print(f"[red]{prompt_template} has no {{message}} placeholder[/]")
            raise typer.Exit(code=2)
        options.prompt_template = template

    def on_record(record: CommitRecord) -> None:
        if record.status == "skipped" and record.reason != "max tasks reached":
            return
        label = {"kept": "[green]kept[/]", "rejected": "[yellow]rejected[/]"}.get(
            record.status, "[dim]skipped[/]"
        )
        reason = f"  [dim]{escape(record.reason)}[/]" if record.reason else ""
        console.print(
            f"  {label} {record.commit[:10]} {escape(record.subject[:70])}{reason}", soft_wrap=True
        )

    console.print(
        f"Mining {escape(str(repo))} ({escape(rev)}, up to {max_commits} commits"
        + (", validating fail-to-pass" if validate else ", without validation")
        + ")",
        soft_wrap=True,
    )
    try:
        report = mine_repository(repo, out, options, force=force, on_record=on_record)
    except MiningError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc

    console.print()
    console.print(f"[bold]{report.kept} task(s) kept[/] from {report.scanned} commit(s) scanned")
    counts = report.reason_counts()
    if counts:
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("not kept because")
        table.add_column("commits", justify="right")
        for reason, count in counts.items():
            table.add_row(reason, str(count))
        console.print(table)
    console.print(f"suite:  {escape(str(report.suite))}", soft_wrap=True)
    console.print(f"report: {escape(str(Path(out) / 'mining_report.json'))}", soft_wrap=True)
    if report.kept:
        suite_path = escape(shlex.quote(str(report.suite)))
        console.print("\nNext:")
        console.print(f"  harnesslab suite check {suite_path}", soft_wrap=True)
        console.print(
            f"  harnesslab run {suite_path} --variants fake-reference,fake-noop", soft_wrap=True
        )
        console.print("  then add your own variants and compare them (or `harnesslab ablate run`)")
    else:
        console.print(
            "[yellow]No task kept.[/] Check the reasons above; --test-command, --test-glob or "
            "--setup usually need adjusting to the project."
        )


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
    _print_routing(routing_gap(samples, task_keys, variant_keys))

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


@experiment_app.command("compare")
def experiment_compare(
    ctx: typer.Context,
    experiment_id: Annotated[str, typer.Argument()],
    a: Annotated[str, typer.Argument(help="Baseline variant id (A).")],
    b: Annotated[str, typer.Argument(help="Variant compared against A (B).")],
    resamples: Annotated[int, typer.Option("--resamples", min=100)] = 2000,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    min_tasks: Annotated[int, typer.Option("--min-tasks", min=1)] = 5,
    metric: Annotated[
        MetricChoice, typer.Option("--metric", help=METRIC_HELP)
    ] = MetricChoice.pass_rate,
) -> None:
    """Paired, task-level comparison of two variants: bootstrap intervals and a sign test."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        exp = Repository(db, settings.home).find_experiment(experiment_id)
        if exp is None:
            err_console.print(f"[red]experiment not found: {experiment_id}[/]")
            raise typer.Exit(code=1)
        samples = samples_from_rows(
            exp.runs, {t.id: t for t in exp.tasks}, {v.id: v for v in exp.variants}
        )
    finally:
        db.dispose()
    keys = [v.variant_key for v in exp.variants]
    for name in (a, b):
        if name not in keys:
            err_console.print(
                f"[red]unknown variant {name!r}; experiment has: {', '.join(keys)}[/]"
            )
            raise typer.Exit(code=2)
    result = paired_comparison(
        samples,
        a,
        b,
        task_order=[t.task_key for t in exp.tasks],
        resamples=resamples,
        seed=seed,
        min_tasks=min_tasks,
        metric=metric.value,
    )
    _print_paired(result)
    table = Table(box=None, padding=(0, 1))
    columns = ["task", f"{a} pass", f"{b} pass", "Δ"]
    if result.metric != "pass_rate":
        columns += [f"{a} {result.metric}", f"{b} {result.metric}", f"Δ {result.metric}"]
    for col in columns:
        table.add_column(col, justify="left" if col == "task" else "right")
    for t in result.tasks:
        row = [
            t.task_key,
            f"{t.a_rate:.0%}",
            f"{t.b_rate:.0%}",
            f"{(t.b_rate - t.a_rate) * 100:+.0f}",
        ]
        if result.metric != "pass_rate":
            va, vb = t.metric_values(result.metric)
            delta = _signed(vb - va, 2) if va is not None and vb is not None else "—"
            row += [_fmt(va), _fmt(vb), delta]
        table.add_row(*row)
    console.print(table)


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
        + f" · verdicts on {METRIC_LABELS[report.verdict_metric]}"
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
            if w.vs_runner_up:
                _print_paired(w.vs_runner_up, title="  evidence vs runner-up")
            if w.vs_baseline:
                _print_paired(w.vs_baseline, title=f"  evidence vs baseline {w.baseline}")
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
        _print_routing(w.routing, indent="  ")
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
    except (SpecError, ValueError) as exc:
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


# ---------------------------------------------------------------------------
# grow
# ---------------------------------------------------------------------------


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _print_grow_report(report: GrowReport) -> None:
    console.print(
        f"[bold]grow {report.name}[/]  session={report.session_id}  status={report.status}"
        f"  phase={report.phase}  iterations={report.iterations}"
        f"  runs={report.runs_started}  deployed cost ${report.deployed_cost_usd:,.4f}"
        f"  optimizer cost ${report.optimizer_cost_usd:,.4f}"
    )
    if report.initial and report.current:
        console.print(
            f"gate pass rate: {_fmt_rate(report.initial.gate_pass_rate)} (v{report.initial.number})"
            f" → {_fmt_rate(report.current.gate_pass_rate)} (v{report.current.number})"
            f" · median {report.minimize}: {_fmt(report.initial.llm_calls_median, 0)}"
            f" → {_fmt(report.current.llm_calls_median, 0)}"
        )
    table = Table(title="harness versions", box=None, padding=(0, 1))
    for col in (
        "version",
        "status",
        "window fixed",
        "gate pass",
        "gate n",
        "llm calls",
        "cost",
        "opt cost",
        "reason",
    ):
        table.add_column(
            col,
            justify="right"
            if col in ("gate pass", "gate n", "llm calls", "cost", "opt cost")
            else "left",
        )
    for v in report.versions:
        colour = {
            "accepted": "green",
            "rejected": "red",
            "invalid": "yellow",
            "initial": "cyan",
        }.get(v.status, "white")
        fixed = f"{len(v.window_fixed)}/{len(v.window_task_keys)}" if v.window_task_keys else "—"
        table.add_row(
            f"v{v.number}",
            f"[{colour}]{v.status}[/]",
            fixed,
            _fmt_rate(v.gate_pass_rate),
            str(v.gate_n_valid) if v.gate_n_valid is not None else "—",
            _fmt(v.llm_calls_median, 0),
            _fmt(v.cost_median, 4),
            _fmt(v.optimizer_cost_usd, 4),
            (v.reason or "")[:80],
        )
    console.print(table)
    if report.final:
        ft = Table(
            title=f"final holdout ({', '.join(report.final.task_keys)})", box=None, padding=(0, 1)
        )
        for col in ("version", "pass rate", "valid", "llm calls", "cost"):
            ft.add_column(col, justify="right" if col != "version" else "left")
        for side in (report.final.initial, report.final.current):
            ft.add_row(
                f"v{side.version_number}",
                _fmt_rate(side.pass_rate),
                str(side.n_valid),
                _fmt(side.llm_calls_median, 0),
                _fmt(side.cost_median, 4),
            )
        console.print(ft)
    for note in report.notes:
        if not note.startswith("Traceback"):
            console.print(f"[dim]note: {note}[/]")


def _grow_progress(p: GrowProgress) -> None:
    if p.kind == "version":
        colour = (
            "green" if "accepted" in p.message else ("red" if "rejected" in p.message else "yellow")
        )
        console.print(f"[{colour}]{p.message}[/]")
    else:
        console.print(f"[dim]▶ {p.message}[/]")


@grow_app.command("list")
def grow_list(ctx: typer.Context) -> None:
    """List grow sessions and bundled grow templates."""
    for name, path in list_bundled_grows().items():
        console.print(f"[bold]{name}[/]  [dim]{path}[/]  (bundled template)")
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        rows = Repository(db, settings.home).list_grow_sessions()
    finally:
        db.dispose()
    table = Table()
    for col in (
        "id",
        "name",
        "created",
        "suite",
        "status",
        "phase",
        "iterations",
        "versions",
        "accepted",
    ):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["id"],
            r["name"],
            r["created_at"].strftime("%Y-%m-%d %H:%M"),
            r["suite_name"],
            r["status"],
            r["phase"] or "—",
            str(r["iterations"]),
            str(r["n_versions"]),
            str(r["n_accepted"]),
        )
    console.print(table)


def _synthetic_context(spec, suite, split, bundle: HarnessBundle) -> OptimizerContext:
    cases = [
        FailureCase(
            task_id=t,
            task_name=t,
            prompt="(task prompt)",
            attempts=0,
            run_id="(dry-run)",
            status="completed",
            outcome="fail",
            verified_score=0.0,
            final_message="",
            trace_digest=["(no runs yet)"],
            diff="",
            verifier_stdout="",
            verifier_stderr="",
            metrics=FailureMetrics(),
        )
        for t in split.train[: spec.window.size]
    ]
    return OptimizerContext(
        session_name=spec.name,
        iteration=1,
        runner=spec.base_variant["runner"],
        model=spec.base_variant.get("model"),
        bundle=bundle.content_files,
        constraints=EditConstraintsSpec(
            allowed_paths=allowed_paths(spec.optimizer.allow_hooks),
            max_files=spec.optimizer.max_files,
            max_file_bytes=EditConstraints().max_file_bytes,
            max_bundle_bytes=EditConstraints().max_bundle_bytes,
        ),
        failures=cases,
        previous_rejections=[],
        suite_description=suite.description,
    )


@grow_app.command("run")
def grow_run(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="A grow.yaml or a bundled grow name (e.g. demo-fake).")
    ],
    max_iterations: Annotated[int | None, typer.Option("--max-iterations", min=1)] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Session name.")] = None,
    keep_worktrees: Annotated[bool, typer.Option("--keep-worktrees")] = False,
    pricing: Annotated[
        Path | None, typer.Option("--pricing", help="pricing.yaml for cost estimates.")
    ] = None,
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", help="Python module registering custom runners or optimizers."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Print the split, window and first optimizer view; run nothing."
        ),
    ] = False,
) -> None:
    """Grow a harness: failures -> optimizer -> window check -> gate check -> accept or roll back."""
    settings = _settings(ctx)
    try:
        spec, suite, tasks, split = load_grow_target(target)
        _load_plugins_or_exit(list(plugin or []) + suite.plugins + spec.plugins)
    except SpecError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    if max_iterations is not None:
        spec.max_iterations = max_iterations
    if name:
        spec.name = name
    if keep_worktrees:
        spec.keep_worktrees = True
    bundle = (
        HarnessBundle.load(spec.harness_dir)
        if spec.harness_dir
        else HarnessBundle(path=Path("."), files={})
    )
    console.print(
        f"[bold]{spec.name}[/]: train {len(split.train)} · gate {len(split.gate)} · final {len(split.final)}"
        f" · window size {spec.window.size} (Q={spec.window.min_fixed}, R_max={spec.window.max_attempts})"
        f" · max {spec.max_iterations} iteration(s) · optimizer {spec.optimizer.kind}"
        f"{' (' + spec.optimizer.model + ')' if spec.optimizer.model else ''}"
        f" · initial harness {bundle.hash[:12]} ({len(bundle.files)} file(s))"
    )
    if dry_run:
        console.print(f"train: {', '.join(split.train)}")
        console.print(f"gate:  {', '.join(split.gate)}")
        console.print(f"final: {', '.join(split.final) or '(none)'}")
        console.print("[bold]first optimizer view (synthetic failures):[/]")
        console.print(_synthetic_context(spec, suite, split, bundle).model_dump_json(indent=2))
        return
    db = _open_db(settings)
    service = GrowService(settings, db, pricing=_load_pricing(settings, pricing))
    try:
        outcome = asyncio.run(service.run(spec, suite, tasks, split, progress=_grow_progress))
        session = service.repo.get_grow_session(outcome.session_id)
        report = report_for_session(service.repo, session)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        err_console.print("[red]interrupted; resume with: harnesslab grow resume <session id>[/]")
        raise typer.Exit(code=130) from None
    except GrowError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    finally:
        db.dispose()
    console.print()
    _print_grow_report(report)
    console.print(
        f"session id: [bold]{outcome.session_id}[/]  ·  harnesslab grow show {outcome.session_id}  ·  harnesslab serve"
    )


@grow_app.command("resume")
def grow_resume(ctx: typer.Context, session_id: Annotated[str, typer.Argument()]) -> None:
    """Continue an interrupted or failed grow session from its saved state."""
    settings = _settings(ctx)
    db = _open_db(settings)
    service = GrowService(settings, db)
    try:
        outcome = asyncio.run(service.resume(session_id, progress=_grow_progress))
        report = report_for_session(service.repo, service.repo.get_grow_session(outcome.session_id))
    except GrowError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    finally:
        db.dispose()
    _print_grow_report(report)
    console.print(f"session id: [bold]{outcome.session_id}[/]")


@grow_app.command("show")
def grow_show(ctx: typer.Context, session_id: Annotated[str, typer.Argument()]) -> None:
    """Print the version lineage and gate results of a grow session."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        repo = Repository(db, settings.home)
        session = repo.find_grow_session(session_id)
        if session is None:
            err_console.print(f"[red]grow session not found: {session_id}[/]")
            raise typer.Exit(code=1)
        report = report_for_session(repo, session)
    finally:
        db.dispose()
    _print_grow_report(report)


@grow_app.command("export")
def grow_export(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument()],
    directory: Annotated[Path, typer.Argument(help="Directory to write the current bundle into.")],
) -> None:
    """Copy the session's current accepted bundle to a directory and write lineage.json."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        repo = Repository(db, settings.home)
        session = repo.find_grow_session(session_id)
        if session is None:
            err_console.print(f"[red]grow session not found: {session_id}[/]")
            raise typer.Exit(code=1)
        current = (
            repo.get_harness_version(session.current_version_id)
            if session.current_version_id
            else None
        )
        if current is None:
            err_console.print("[red]session has no current harness version[/]")
            raise typer.Exit(code=1)
        report = report_for_session(repo, session)
    finally:
        db.dispose()
    directory = directory.expanduser().resolve()
    HarnessBundle.load(settings.home / current.bundle_path).write_to(directory)
    (directory / "lineage.json").write_text(json.dumps(lineage_json(report), indent=2, default=str))
    console.print(
        f"wrote v{current.number} ({current.harness_hash[:12]}) and lineage.json to {directory}"
    )


# ---------------------------------------------------------------------------
# ablate
# ---------------------------------------------------------------------------


def _print_ablation(report: AblationReport) -> None:
    console.print(
        f"[bold]ablation[/] of {report.bundle} (hash {report.bundle_hash[:12]}) on {report.base_variant}"
    )
    for note in report.notes:
        console.print(f"[dim]note: {note}[/]")
    _print_paired(report.full_vs_minimal, title="whole bundle (full vs minimal)")
    metric = report.full_vs_minimal.metric
    # The verdict column is about the ablation's metric, so its interval is shown next to it.
    scale, digits, unit = (100.0, 1, " (pts)") if metric == "pass_rate" else (1.0, 2, "")
    table = Table(title="component effects: full minus without-component", box=None, padding=(0, 1))
    for col in (
        "component",
        "verdict",
        f"Δ {METRIC_LABELS[metric]}{unit}",
        "interval",
        "Δ cost",
        "Δ llm_calls",
        "W/L/T",
    ):
        table.add_column(col, justify="left" if col in ("component", "verdict") else "right")
    colours = {"helps": "green", "hurts": "red"}
    for e in report.components:
        c = e.comparison
        iv, cost, calls = c.metric_diff, c.cost_diff, c.llm_calls_diff
        table.add_row(
            e.component,
            f"[{colours.get(e.verdict, 'yellow')}]{e.verdict}[/]",
            _signed(iv.estimate * scale, digits) if iv else "—",
            f"[{_signed(iv.low * scale, digits)}, {_signed(iv.high * scale, digits)}]"
            if iv
            else "—",
            f"{cost.estimate:+.4f}" if cost else "—",
            f"{calls.estimate:+.2f}" if calls else "—",
            f"{c.wins}/{c.losses}/{c.ties}",
        )
    console.print(table)
    console.print(
        "[dim]A component 'helps' only when the paired interval of (full − without) excludes zero "
        "over enough tasks; 'no evidence' means keep the simpler harness unless it is cheaper.[/]"
    )


@ablate_app.command("run")
def ablate_run(
    ctx: typer.Context,
    bundle_dir: Annotated[Path, typer.Argument(help="Harness bundle directory to ablate.")],
    suite: Annotated[str, typer.Option("--suite", help="Suite path or bundled suite name.")],
    variant: Annotated[
        str,
        typer.Option(
            "--variant", help="Base variant (runner, model, options) the bundle is applied to."
        ),
    ] = "claude-default",
    tasks: Annotated[
        str | None, typer.Option("--tasks", "-t", help="Comma-separated task ids (default: all).")
    ] = None,
    repetitions: Annotated[int, typer.Option("--repetitions", "-r", min=1)] = 2,
    parallelism: Annotated[int, typer.Option("--parallelism", "-p", min=1)] = 2,
    name: Annotated[str | None, typer.Option("--name", "-n", help="Experiment name.")] = None,
    min_tasks: Annotated[int, typer.Option("--min-tasks", min=1)] = 5,
    resamples: Annotated[int, typer.Option("--resamples", min=100)] = 2000,
    seed: Annotated[int, typer.Option("--seed")] = 0,
    metric: Annotated[
        MetricChoice, typer.Option("--metric", help=METRIC_HELP)
    ] = MetricChoice.pass_rate,
    keep_worktrees: Annotated[bool, typer.Option("--keep-worktrees")] = False,
    pricing: Annotated[
        Path | None, typer.Option("--pricing", help="pricing.yaml for cost estimates.")
    ] = None,
    plugin: Annotated[
        list[str] | None, typer.Option("--plugin", help="Python module registering custom runners.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the components and variants, run nothing.")
    ] = False,
) -> None:
    """Run the full bundle, an empty (minimal) bundle and one leave-one-out bundle per component."""
    settings = _settings(ctx)
    try:
        suite_path = resolve_suite_target(suite)
        suite_spec, all_tasks = load_suite(suite_path)
        _load_plugins_or_exit(list(plugin or []) + suite_spec.plugins)
        chosen = select_tasks(all_tasks, [t.strip() for t in tasks.split(",")] if tasks else None)
        holder = ExperimentSpec(name="ablate", suite=str(suite_path), source_path=suite_path)
        base = resolve_variants([variant], holder, suite_spec)[0]
        bundle = HarnessBundle.load(bundle_dir)
        out_dir = settings.home / "ablations" / bundle.hash[:16]
        variants, spec = plan_ablation(
            bundle_dir,
            base,
            out_dir,
            resamples=resamples,
            seed=seed,
            min_tasks=min_tasks,
            metric=metric.value,
        )
    except (SpecError, BundleError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    total = len(variants) * len(chosen) * repetitions
    console.print(
        f"[bold]{name or 'ablate ' + bundle_dir.name}[/]: {len(spec.components)} component(s) "
        f"({', '.join(spec.components)}) -> {len(variants)} variant(s) x {len(chosen)} task(s) x "
        f"{repetitions} repetition(s) = {total} run(s) on base variant {base.id}; "
        f"verdicts on {METRIC_LABELS[spec.metric]}"
    )
    if len(chosen) < min_tasks:
        console.print(
            f"[yellow]{len(chosen)} task(s) < --min-tasks {min_tasks}: the report will show intervals "
            "but no verdicts.[/]"
        )
    if dry_run:
        for v in variants:
            console.print(f"  {v.id}  [dim]{v.harness}[/]")
        return
    experiment = ExperimentSpec(
        name=name or f"ablate {bundle_dir.name}",
        suite=str(suite_path),
        repetitions=repetitions,
        parallelism=parallelism,
        variants=variants,
        keep_worktrees=keep_worktrees,
        plugins=suite_spec.plugins,
        ablation=spec.model_dump(mode="json"),
        source_path=suite_path,
    )
    db = _open_db(settings)
    service = ExperimentService(settings, db, pricing=_load_pricing(settings, pricing))
    try:
        outcome = asyncio.run(
            service.run_experiment(
                experiment,
                suite_spec,
                chosen,
                variants,
                keep_worktrees=keep_worktrees,
                progress=_progress_printer(total),
            )
        )
        report = ablation_report_for(service.repo.get_experiment(outcome.experiment_id))
    finally:
        db.dispose()
    console.print()
    if report is not None:
        _print_ablation(report)
    console.print(
        f"experiment id: [bold]{outcome.experiment_id}[/]  ·  harnesslab ablate report "
        f"{outcome.experiment_id}  ·  harnesslab serve"
    )


@ablate_app.command("report")
def ablate_report(ctx: typer.Context, experiment_id: Annotated[str, typer.Argument()]) -> None:
    """Recompute the component verdicts of a past ablation from the database."""
    settings = _settings(ctx)
    db = _open_db(settings)
    try:
        exp = Repository(db, settings.home).find_experiment(experiment_id)
        if exp is None:
            err_console.print(f"[red]experiment not found: {experiment_id}[/]")
            raise typer.Exit(code=1)
        report = ablation_report_for(exp)
    finally:
        db.dispose()
    if report is None:
        err_console.print(f"[red]experiment {exp.id} is not an ablation[/]")
        raise typer.Exit(code=1)
    _print_ablation(report)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@harness_app.command("check")
def harness_check(
    ctx: typer.Context,
    bundle_dir: Annotated[Path, typer.Argument(help="Harness bundle directory.")],
    suite: Annotated[
        str | None,
        typer.Option("--suite", help="Suite (path or bundled name) to run the leak lint against."),
    ] = None,
) -> None:
    """Validate a harness bundle and, with --suite, lint it for hidden-test leaks."""
    try:
        bundle = HarnessBundle.load(bundle_dir)
    except BundleError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold]{bundle.name or bundle_dir.name}[/]  hash {bundle.hash}")
    for entry in bundle.file_summary():
        console.print(f"  {entry['path']}  [dim]{entry['bytes']} bytes[/]")
    if "hooks.json" in bundle.files:
        console.print(
            "[yellow]warning:[/] hooks.json runs shell commands on this machine outside the "
            "agent's tool allowlist (Claude Code); review every command before using the bundle"
        )
    if suite:
        try:
            _, tasks = load_suite(resolve_suite_target(suite))
        except SpecError as exc:
            err_console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=2) from exc
        secrets = SuiteSecrets.from_tasks(tasks)
        content = bundle.content_files
        # A human-authored bundle may carry hooks; only optimizers are barred from writing them.
        errors = lint_candidate(
            {}, content, secrets, EditConstraints(max_files=10**6, allow_hooks=True)
        )
        if errors:
            for error in errors:
                console.print(f"[red]✘ {error}[/]")
            raise typer.Exit(code=1)
        console.print(f"[green]ok[/] no leaks against suite {suite} ({len(tasks)} task(s))")
    else:
        console.print("[green]ok[/] valid bundle (pass --suite to lint for hidden-test leaks)")


if __name__ == "__main__":  # pragma: no cover
    app()

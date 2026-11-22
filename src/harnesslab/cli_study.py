"""``harnesslab study``: run a study through a domain adapter.

The order is fixed: ``check``, then ``search`` (dev tasks only), then ``freeze`` (once), then
``heldout`` (once), then ``report``. Freezing and the held-out phase cannot be undone, and paid
adapters spend money, so outside a terminal those three need ``--yes``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

study_app = typer.Typer(
    help="Studies: search on dev tasks, freeze the choice, compare once on held-out tasks.",
    no_args_is_help=True,
)

SpecArg = Annotated[Path, typer.Argument(help="The study YAML (next to its split.json).")]
YesOpt = Annotated[
    bool,
    typer.Option(
        "--yes", help="Do not ask (required outside a terminal for irreversible or paid steps)."
    ),
]


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _open(path: Path) -> Any:
    from harnesslab.domains import ensure_builtin_domains
    from harnesslab.study.protocol import Study
    from harnesslab.study.registry import create_domain
    from harnesslab.study.spec import load_study_spec

    spec = load_study_spec(path)
    ensure_builtin_domains()
    adapter = create_domain(spec.adapter, spec.adapter_options, spec.study_dir)
    try:
        return Study(spec, adapter, log=lambda m: console.print(escape(m)))
    except BaseException:
        _close_adapter(adapter)
        raise


@contextlib.contextmanager
def _study(path: Path) -> Iterator[Any]:
    """Open a study, turn protocol errors into messages, and always close the adapter."""
    from harnesslab.study.protocol import ProtocolError
    from harnesslab.study.runner import HarnessError
    from harnesslab.study.spec import StudySpecError
    from harnesslab.study.store import DuplicateRecordError, FreezeExistsError, StudyLockedError

    study = None
    try:
        study = _open(path)
        yield study
    except (StudySpecError, ProtocolError, StudyLockedError, FreezeExistsError) as exc:
        err_console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc
    except (HarnessError, DuplicateRecordError) as exc:
        err_console.print(f"[red]harness error, the phase stopped: {escape(str(exc))}[/]")
        raise typer.Exit(code=3) from exc
    finally:
        if study is not None:
            _close_adapter(study.adapter)


def _confirm(question: str, *, yes: bool) -> None:
    if yes:
        return
    if not _interactive():
        err_console.print(f"[red]{escape(question)} Not a terminal: pass --yes to confirm.[/]")
        raise typer.Exit(code=1)
    if not typer.confirm(question, default=False):
        err_console.print("Aborted; nothing was run.")
        raise typer.Exit(code=1)


def _close_adapter(adapter: Any) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        asyncio.run(close())


@study_app.command("check")
def study_check(spec: SpecArg) -> None:
    """Check the split, the spec and the dev tasks before running anything."""
    with _study(spec) as study:
        problems = study.check()
        console.print(
            f"{study.spec.name}: {len(study.dev_ids)} dev and {len(study.held_out_ids)} held-out "
            f"tasks, adapter {study.adapter.name} {study.adapter.version}, {study.space.size} "
            "configurations in the search space"
        )
        for p in problems:
            console.print(f"[red]problem:[/] {escape(p)}")
        if problems:
            raise typer.Exit(code=1)
        console.print("[green]no problems found[/]")


@study_app.command("search")
def study_search(
    spec: SpecArg,
    stage: Annotated[
        list[str] | None, typer.Option("--stage", help="Run only these stages (repeatable).")
    ] = None,
    yes: YesOpt = False,
) -> None:
    """Run search stages on dev tasks (cells already recorded are skipped)."""
    with _study(spec) as study:
        names = [s.name for s in study.spec.search.stages]
        unknown = [s for s in stage or [] if s not in names]
        if unknown:
            err_console.print(f"[red]unknown stage(s) {unknown}; the spec has {names}[/]")
            raise typer.Exit(code=2)
        if getattr(study.adapter, "paid", False):
            b = study.spec.budget
            cap = f"${b.max_cost_usd:g}" if b.max_cost_usd is not None else "none"
            _confirm(
                f"This runs {study.adapter.name}, which spends money (study cap {cap}, "
                f"held-out reserve ${b.held_out_reserve_usd:g}). Start?",
                yes=yes,
            )
        out = asyncio.run(study.search(stage))
    for name, result in out.items():
        if isinstance(result, str):
            console.print(f"stage {name}: {escape(result)}")
        else:
            console.print(
                f"stage {name}: ran {result.ran}, already done {result.already_done}, not started "
                f"{result.not_started}, verdicts {result.verdicts}, charged ${result.cost_usd:.4f}"
                + (f" [yellow]({escape(result.budget_stop)})[/]" if result.budget_stop else "")
            )


@study_app.command("freeze")
def study_freeze(spec: SpecArg, yes: YesOpt = False) -> None:
    """Write freeze.json once, from the dev ranking. Commit it before running heldout."""
    with _study(spec) as study:
        _freeze(study, yes=yes)


def _freeze(study: Any, *, yes: bool) -> None:
    ranking = study.rank(eligible=study.freeze_eligible())
    table = Table("rank", "label", "feasible", "score", "note")
    for s in ranking.scores[:8]:
        score = (
            f"{s.pass_rate:.3f}"
            if s.pass_rate is not None and ranking.kind == "pass_rate"
            else ", ".join(f"{k} {v:.4f}" for k, v in s.ratios.items())
        )
        table.add_row(
            str(s.rank or "-"),
            escape(s.label or s.config_hash[:12]),
            str(s.feasible),
            score,
            escape(s.reason),
        )
    console.print(table)
    if ranking.dropped_tasks:
        console.print(f"dev tasks left out: {ranking.dropped_tasks}")
    open_cells = study.interrupted("dev")["without_record"]
    if open_cells:
        console.print(
            f"[yellow]{len(open_cells)} interrupted dev cell(s) never ran again (resume the search "
            f"to complete them): {escape(', '.join(open_cells[:5]))}[/]"
        )
    best = ranking.best
    if best is None:
        err_console.print("[red]no feasible configuration: nothing to freeze[/]")
        raise typer.Exit(code=1)
    _confirm(f"Freeze {best.label} ({best.config_hash[:12]})? This cannot be undone.", yes=yes)
    data = study.freeze()
    console.print(f"wrote {study.store.freeze_path} (sha256 {data['freeze_sha256'][:16]})")
    console.print("[yellow]Commit freeze.json before running `harnesslab study heldout`.[/]")


@study_app.command("heldout")
def study_heldout(spec: SpecArg, yes: YesOpt = False) -> None:
    """Run the frozen comparison on the held-out tasks, once."""
    with _study(spec) as study:
        paid = " and spends money" if getattr(study.adapter, "paid", False) else ""
        _confirm(f"Run the held-out phase of {study.spec.name}? It runs once{paid}.", yes=yes)
        summary = asyncio.run(study.held_out())
    console.print(
        f"held-out: ran {summary.ran}, already done {summary.already_done}, not started "
        f"{summary.not_started}, verdicts {summary.verdicts}, charged ${summary.cost_usd:.4f}"
        + (f" [yellow]({escape(summary.budget_stop)})[/]" if summary.budget_stop else "")
    )


@study_app.command("report")
def study_report(
    spec: SpecArg,
    as_json: Annotated[bool, typer.Option("--json", help="Print results.json to stdout.")] = False,
) -> None:
    """Compute results.json from the records (paired statistics on held-out tasks)."""
    with _study(spec) as study:
        data = study.results()
        path = study.store.results_path
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return
    console.print(f"wrote {path}")
    for p in (data.get("audit") or {}).get("freeze_problems") or []:
        console.print(f"[red]freeze problem:[/] {escape(p)}")
    ho = data.get("held_out")
    if not ho:
        console.print("no held-out results yet")
        return
    if not ho["complete"]:
        console.print(
            f"[yellow]INCOMPLETE: {ho['missing_cells']} of {ho['expected_cells']} held-out cells "
            "have no record; tasks missing a cell are left out of each comparison[/]"
        )
    _print_comparisons(ho["comparisons"])
    for tag, group in (ho.get("groups") or {}).items():
        console.print(f"[bold]tag {escape(tag)}[/] ({len(group['tasks'])} tasks)")
        _print_comparisons(group["comparisons"])


def _print_comparisons(comparisons: list[dict[str, Any]]) -> None:
    for c in comparisons:
        if "rate_a" in c:
            iv = c["diff"] or {}
            span = (
                f"diff {iv['estimate']:+.3f} [{iv['low']:+.3f}, {iv['high']:+.3f}]"
                if iv
                else "diff -"
            )
            console.print(
                f"{escape(c['name'])}: n={c['n']} rate {c['rate_a']} -> {c['rate_b']}, {span}, "
                f"W/L/T {c['wins']}/{c['losses']}/{c['ties']}, sign p={c['sign_test_p']}, "
                f"McNemar p={c['mcnemar_majority_p']}, not run {len(c['not_run'])}"
            )
        else:
            iv = c["mean_diff"]
            span = f"[{iv['low']:+.3f}, {iv['high']:+.3f}]" if iv else "[-]"
            est = f"{iv['estimate']:+.3f}" if iv else "-"
            console.print(
                f"{escape(c['name'])}: n={c['n']} mean diff {est} {span}, "
                f"W/L/T {c['wins']}/{c['losses']}/{c['ties']}, unverified losses "
                f"{len(c['losses_unverified'])}, sign p (failures as losses)="
                f"{c['sign_test_p_with_failures']}, excluded {len(c['excluded_baseline_unverified'])}"
                f", not run {len(c['not_run'])}"
            )


@study_app.command("status")
def study_status(spec: SpecArg) -> None:
    """Records, cost and budget per phase."""
    with _study(spec) as study:
        console.print_json(json.dumps(study.status()))

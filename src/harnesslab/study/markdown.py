"""Render a study's ``results.json`` as Markdown tables, for a human-written report to cite.

Only numbers that are in ``results.json`` (and the freeze summary it carries) appear, each table
row labelled with the comparison name used in the JSON, so a sentence in a report can be traced
to a line here and from there to the records. No narrative and no claims: the report's author
writes those.
"""

from __future__ import annotations

from typing import Any


def _fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.{digits}g}"
    return str(v)


def _interval(iv: dict[str, Any] | None, digits: int = 4) -> str:
    if not iv:
        return "–"
    return (
        f"{_fmt(iv.get('estimate'), digits)} "
        f"[{_fmt(iv.get('low'), digits)}, {_fmt(iv.get('high'), digits)}]"
    )


def _metric_table(comparisons: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Comparison | n | Mean A | Mean B | Mean diff (B − A) [95% CI] | Median diff | "
        "Ratio B/A [95% CI] | W/L/T | Sign p | Unverified losses | Sign p (losses counted) | "
        "Excluded | Not run |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in comparisons:
        lines.append(
            f"| {c['name']} | {c['n']} | {_fmt(c.get('mean_a'))} | {_fmt(c.get('mean_b'))} | "
            f"{_interval(c.get('mean_diff'))} | {_fmt(c.get('median_diff'))} | "
            f"{_interval(c.get('ratio'))} | {c['wins']}/{c['losses']}/{c['ties']} | "
            f"{_fmt(c.get('sign_test_p'))} | {len(c.get('losses_unverified') or [])} | "
            f"{_fmt(c.get('sign_test_p_with_failures'))} | "
            f"{len(c.get('excluded_baseline_unverified') or [])} | {len(c.get('not_run') or [])} |"
        )
    return lines


def _rate_table(comparisons: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Comparison | n | Rate A | Rate B | Diff (B − A) [95% CI] | W/L/T | Sign p | "
        "Majority only B / only A | McNemar p | Not run |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in comparisons:
        lines.append(
            f"| {c['name']} | {c['n']} | {_fmt(c.get('rate_a'))} | {_fmt(c.get('rate_b'))} | "
            f"{_interval(c.get('diff'))} | {c['wins']}/{c['losses']}/{c['ties']} | "
            f"{_fmt(c.get('sign_test_p'))} | {c.get('majority_only_b')} / "
            f"{c.get('majority_only_a')} | {_fmt(c.get('mcnemar_majority_p'))} | "
            f"{len(c.get('not_run') or [])} |"
        )
    return lines


def _comparisons(comparisons: list[dict[str, Any]]) -> list[str]:
    if not comparisons:
        return ["(no comparison)"]
    if "rate_a" in comparisons[0]:
        return _rate_table(comparisons)
    return _metric_table(comparisons)


def render_markdown(results: dict[str, Any]) -> str:
    out: list[str] = [f"# Study `{results['study']}`: tables from results.json", ""]
    out.append(
        f"Generated {results.get('generated_at')} by harnesslab {results.get('harnesslab_version')}."
    )
    freeze = results.get("freeze") or {}
    if freeze:
        out += [
            "",
            "## Freeze",
            "",
            f"- frozen at {freeze.get('frozen_at')}, freeze_sha256 `{freeze.get('freeze_sha256')}`",
            f"- git head at the freeze `{freeze.get('git_head')}`, spec sha256 "
            f"`{freeze.get('spec_sha256')}`",
            f"- baseline `{freeze['baseline']['config_hash'][:12]}`: "
            f"`{freeze['baseline']['config']}`",
            f"- frozen `{freeze['frozen'].get('label')}` "
            f"`{freeze['frozen']['config_hash'][:12]}`: `{freeze['frozen']['config']}`",
        ]
        for c in freeze.get("comparators") or []:
            out.append(f"- comparator `{c['label']}` `{c['config_hash'][:12]}`")
    dev = results.get("dev")
    if dev:
        out += [
            "",
            "## Search (dev)",
            "",
            f"- records {dev['records']}, configurations {dev['configurations']}, per stage "
            f"{dev['records_per_stage']}",
            f"- cost ${_fmt(dev.get('charged_usd'))}, cell time {_fmt(dev.get('cell_seconds'))} s",
            f"- verdicts {dev['verdicts']}",
            f"- considered tasks {len(dev['considered_tasks'])}; left out: "
            + (
                ", ".join(f"{t} ({why})" for t, why in dev["dropped_tasks"].items())
                if dev["dropped_tasks"]
                else "none"
            ),
            f"- feasible configurations {dev['feasible_configurations']}",
            "",
            "| Rank | Label | Feasible | Pass rate | Ratios | Medians | Note |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for s in dev.get("ranking_top", [])[:10]:
            ratios = ", ".join(f"{k} {_fmt(v, 6)}" for k, v in (s.get("ratios") or {}).items())
            medians = ", ".join(f"{k} {_fmt(v)}" for k, v in (s.get("medians") or {}).items())
            out.append(
                f"| {s.get('rank') or '–'} | {s.get('label')} | {_fmt(s.get('feasible'))} | "
                f"{_fmt(s.get('pass_rate'))} | {ratios or '–'} | {medians or '–'} | "
                f"{s.get('reason') or ''} |"
            )
    ho = results.get("held_out")
    if ho:
        out += [
            "",
            "## Held-out",
            "",
            f"- complete: {_fmt(ho['complete'])} ({ho['recorded_cells']} of "
            f"{ho['expected_cells']} planned cells recorded)",
            "",
            "| Configuration | Records | Passes | Verdicts | Attempt status | Cost $ | "
            "Wall time median s |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for _h, c in sorted(ho["by_config"].items(), key=lambda kv: str(kv[1]["label"])):
            out.append(
                f"| {c['label']} | {c['records']} | {c['passes']} | {c['verdicts']} | "
                f"{c['attempt_status']} | {_fmt(c.get('charged_usd'))} | "
                f"{_fmt((c.get('wall_time_s') or {}).get('median'))} |"
            )
        out += ["", "### All held-out tasks", ""] + _comparisons(ho["comparisons"])
        for tag, group in (ho.get("groups") or {}).items():
            out += ["", f"### Tasks tagged `{tag}` ({len(group['tasks'])})", ""]
            out += _comparisons(group["comparisons"])
        if ho.get("failures"):
            out += ["", "### Cells that did not pass", "", "| Task | Configuration | Attempt | "
                    "Verdict | Reason |", "| --- | --- | --- | --- | --- |"]  # fmt: skip
            for f in ho["failures"]:
                reason = str(f.get("reason") or "").replace("|", "/").replace("\n", " ")[:160]
                out.append(
                    f"| {f['task']} | {f['config']} | {f['status']} | {f['verdict']} | {reason} |"
                )
    audit = results.get("audit") or {}
    if audit:
        out += ["", "## Audit", ""]
        for phase in ("smoke", "dev", "held_out"):
            a = audit.get(phase)
            if a:
                out.append(
                    f"- {phase}: {a['records']} records, {a['harness_error_records']} with a "
                    f"harness error, {a['interrupted']['cells']} interrupted cell(s), charged "
                    f"${_fmt(a['charged_usd'])}"
                )
        for p in audit.get("freeze_problems") or []:
            out.append(f"- freeze problem: {p}")
    return "\n".join(out) + "\n"

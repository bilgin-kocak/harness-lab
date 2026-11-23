"""Reproduce the math study's published numbers from its committed files (no API call).

Usage (with the pinned toolchain: ``setup.sh`` done, ``PATH="$HOME/.elan/bin:$PATH"``)::

    python studies/2026-11-math-minif2f/reproduce.py <miniF2F-lean4 checkout at 5746b7d>
        [--reverify N | --reverify all] [--study-dir DIR] [--skip-split-check]

Steps, each printed with PASS or FAIL:

1. **Split.** ``select_tasks.py --check`` re-derives ``split.json`` from the checkout and the
   recorded seed and must find it unchanged.
2. **Freeze.** It checks ``freeze.json``'s own hash, and every freeze check the held-out phase
   made: split, adapter, provenance, sealed spec sections, dev records, and that re-ranking gives
   the frozen choice. Skipped: that ``freeze.json`` is committed (``git log -- freeze.json``
   shows that), because the scratch copy is not a git checkout.
3. **Report.** ``results.json`` is recomputed from the committed records (``*.jsonl.gz``,
   decompressed into a scratch copy of the study), and every held-out comparison must match.
4. **Complete.** The held-out phase recorded every planned cell.
5. ``--reverify``: the Lean kernel checks the recorded proofs again (N per configuration, or
   all of them), with the adapter's verifier, and each verdict must equal the recorded one.
   Lean is deterministic. No model is called: a new model attempt would cost money, and model
   sampling cannot be seeded, so the attempts themselves are not re-run.

Nothing is written outside a temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPARE_KEYS = (
    "n",
    "tasks",
    "rate_a",
    "rate_b",
    "diff",
    "wins",
    "losses",
    "ties",
    "sign_test_p",
    "majority_only_a",
    "majority_only_b",
    "mcnemar_majority_p",
    "not_run",
)


def _ok(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    return ok


def _scratch_copy(source: Path, dest: Path) -> Path:
    """The study directory with its records decompressed, in ``dest``."""
    study = dest / source.name
    shutil.copytree(source, study, ignore=shutil.ignore_patterns("artifacts", "__pycache__"))
    for gz in study.rglob("*.jsonl.gz"):
        with gzip.open(gz, "rt", encoding="utf-8") as src:
            gz.with_suffix("").write_text(src.read(), encoding="utf-8")
        gz.unlink()
    return study


def _open_study(study_dir: Path):  # noqa: ANN202 - Study
    from harnesslab.domains import ensure_builtin_domains
    from harnesslab.study.protocol import Study
    from harnesslab.study.registry import create_domain
    from harnesslab.study.spec import load_study_spec

    spec = load_study_spec(study_dir / "study.yaml")
    ensure_builtin_domains()
    adapter = create_domain(spec.adapter, spec.adapter_options, spec.study_dir)
    return Study(spec, adapter, log=lambda _m: None)


def _strip(c: dict) -> dict:
    return {k: c.get(k) for k in COMPARE_KEYS}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("minif2f", type=Path)
    ap.add_argument("--reverify", default="0", help="N per configuration, or 'all'")
    ap.add_argument("--study-dir", type=Path, default=HERE)
    ap.add_argument("--skip-split-check", action="store_true")
    args = ap.parse_args(argv)
    source = args.study_dir.resolve()
    ok = True

    if not args.skip_split_check:
        res = subprocess.run(
            [sys.executable, str(HERE / "select_tasks.py"), str(args.minif2f), "--check"],
            capture_output=True,
            text=True,
        )
        ok &= _ok("split.json re-derived from the checkout", res.returncode == 0, res.stdout[-300:])

    with tempfile.TemporaryDirectory() as tmp:
        study_dir = _scratch_copy(source, Path(tmp))
        study = _open_study(study_dir)
        try:
            freeze = study.store.read_freeze()
            if freeze is None:
                return int(not _ok("freeze.json present", False))
            problems = [
                p
                for p in study.freeze_problems(freeze)
                if not p.endswith(("is not committed to git", "differs from the committed version"))
            ]
            ok &= _ok("freeze checks", not problems, "; ".join(problems))
            committed = json.loads((source / "results.json").read_text(encoding="utf-8"))
            fresh = study.results()
            a = [_strip(c) for c in committed["held_out"]["comparisons"]]
            b = [_strip(c) for c in fresh["held_out"]["comparisons"]]
            ok &= _ok(
                "held-out comparisons recomputed from the records", a == b, f"{len(a)} comparisons"
            )
            ok &= _ok(
                "held-out phase complete",
                bool(fresh["held_out"]["complete"]),
                f"{fresh['held_out']['recorded_cells']} of {fresh['held_out']['expected_cells']}",
            )
            spent = sum(
                float(fresh["audit"][phase]["charged_usd"] or 0.0)
                for phase in ("smoke", "dev", "held_out")
                if fresh["audit"].get(phase)
            )
            ok &= _ok("total charged within the $150 cap", spent <= 150.0, f"${spent:.4f}")
            if args.reverify != "0":
                per_config = None if args.reverify == "all" else int(args.reverify)
                ok &= asyncio.run(_reverify(study, per_config))
        finally:
            close = getattr(study.adapter, "close", None)
            if close is not None:
                asyncio.run(close())
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


async def _reverify(study, per_config: int | None) -> bool:  # noqa: ANN001
    from harnesslab.study.contract import Attempt, VerifyContext

    tasks = {t.id: t for t in study.adapter.load_tasks(study.held_out_ids)}
    counts: dict[str, int] = {}
    checked = 0
    differ: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for rec in study.store.effective("held_out"):
            label = rec.get("config_label") or rec["config_hash"][:12]
            if per_config is not None and counts.get(label, 0) >= per_config:
                continue
            counts[label] = counts.get(label, 0) + 1
            attempt = Attempt.model_validate(rec["attempt"])
            verdict = await study.adapter.verify(
                tasks[rec["task_id"]],
                attempt,
                VerifyContext(
                    study=study.spec.name,
                    phase="held_out",
                    attempt_dir=Path(tmp),
                    keep_artifacts=False,
                ),
            )
            checked += 1
            if verdict.status.value != (rec.get("verdict") or {}).get("status"):
                differ.append(f"{rec['task_id']} / {label} rep {rec['repetition']}")
    for item in differ:
        print(f"  verdict differs: {item}")
    return _ok(
        "recorded held-out proofs re-checked by the Lean kernel",
        not differ,
        f"{checked} attempts, {len(differ)} verdicts differ",
    )


if __name__ == "__main__":
    sys.exit(main())

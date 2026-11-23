"""Reproduce the quantum study's published numbers from its committed files.

Usage (with the pinned toolchain: ``uv sync --all-extras``)::

    python studies/2026-11-quantum-qasmbench/reproduce.py <QASMBench checkout at 357b942>
        [--rerun-held-out] [--reverify N] [--study-dir DIR] [--skip-split-check]

``--study-dir`` reproduces another study directory laid out the same way (the end-to-end test
uses it on fixture circuits); ``--skip-split-check`` skips step 1 for a split that
``select_tasks.py`` did not draw.

Steps, each printed with PASS or FAIL:

1. **Split.** ``select_tasks.py --check`` re-derives ``split.json`` from the checkout and the
   recorded seed and must find it unchanged.
2. **Freeze.** ``freeze.json``'s own hash, and every freeze check the held-out phase made (split,
   adapter, provenance, sealed spec sections, dev records, re-ranking gives the frozen choice).
3. **Report.** ``results.json`` is recomputed from the committed records (``*.jsonl.gz``
   decompressed into a scratch copy of the study) and every held-out comparison must match.
4. ``--rerun-held-out``: every held-out cell is compiled again with the adapter (deterministic:
   fixed seeds, canonical QPY) and its output hash must equal the recorded one.
   ``--reverify N`` also re-runs the verifier on N recorded held-out cells per configuration
   and compares verdicts. Re-running reads held-out circuits, which is fine after the freeze.

Nothing is written outside a temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPARE_KEYS = (
    "n",
    "tasks",
    "wins",
    "losses",
    "ties",
    "sign_test_p",
    "sign_test_p_with_failures",
    "mean_diff",
    "median_diff",
    "ratio",
    "losses_unverified",
    "excluded_baseline_unverified",
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


def _open_study(study_dir: Path, qasmbench: Path):  # noqa: ANN202 - Study
    from harnesslab.domains import ensure_builtin_domains
    from harnesslab.study.protocol import Study
    from harnesslab.study.registry import create_domain
    from harnesslab.study.spec import load_study_spec

    os.environ["HARNESSLAB_QASMBENCH_DIR"] = str(qasmbench)
    spec = load_study_spec(study_dir / "study.yaml")
    ensure_builtin_domains()
    adapter = create_domain(spec.adapter, spec.adapter_options, spec.study_dir)
    return Study(spec, adapter, log=lambda _m: None)


def _strip(c: dict) -> dict:
    return {k: c.get(k) for k in COMPARE_KEYS}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("qasmbench", type=Path)
    ap.add_argument("--rerun-held-out", action="store_true")
    ap.add_argument("--reverify", type=int, default=0)
    ap.add_argument("--study-dir", type=Path, default=HERE)
    ap.add_argument("--skip-split-check", action="store_true")
    args = ap.parse_args(argv)
    source = args.study_dir.resolve()
    ok = True

    if not args.skip_split_check:
        res = subprocess.run(
            [sys.executable, str(HERE / "select_tasks.py"), str(args.qasmbench), "--check"],
            capture_output=True,
            text=True,
        )
        ok &= _ok("split.json re-derived from the checkout", res.returncode == 0, res.stdout[-300:])

    with tempfile.TemporaryDirectory() as tmp:
        study_dir = _scratch_copy(source, Path(tmp))
        study = _open_study(study_dir, args.qasmbench)
        try:
            freeze = study.store.read_freeze()
            if freeze is None:
                return int(not _ok("freeze.json present", False))
            # The scratch copy is not a git checkout: only the git check is expected to fail.
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
                "held-out comparisons recomputed from the records",
                a == b,
                f"{len(a)} comparisons",
            )
            ok &= _ok(
                "held-out phase complete",
                bool(fresh["held_out"]["complete"]),
                f"{fresh['held_out']['recorded_cells']} of {fresh['held_out']['expected_cells']}",
            )
            if args.rerun_held_out or args.reverify:
                ok &= asyncio.run(_rerun(study, freeze, args.rerun_held_out, args.reverify))
        finally:
            close = getattr(study.adapter, "close", None)
            if close is not None:
                asyncio.run(close())
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


async def _rerun(study, freeze: dict, rerun: bool, reverify: int) -> bool:  # noqa: ANN001
    from harnesslab.study.contract import RunContext, VerifyContext

    records = {
        (r["task_id"], r["config_hash"], r["repetition"]): r
        for r in study.store.effective("held_out")
    }
    tasks = {t.id: t for t in study.adapter.load_tasks(study.held_out_ids)}
    cells = study.held_out_cells(freeze, list(tasks.values()))
    ok = True
    mismatched = 0
    checked = 0
    per_config: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a").mkdir()
        for cell in cells:
            rec = records.get(cell.key)
            if rec is None:
                continue
            ctx = RunContext(
                study=study.spec.name,
                phase="held_out",
                repetition=cell.repetition,
                attempt_dir=Path(tmp) / "a",
                keep_artifacts=False,
                cost_limit_usd=None,
                seed=rec["seed"],
            )
            if not rerun and per_config.get(cell.config.config_hash, 0) >= reverify:
                continue
            attempt = await study.adapter.run(cell.task, cell.config, ctx)
            same = attempt.output.get("qpy_sha256") == rec["attempt"]["output"].get("qpy_sha256")
            mismatched += not same
            checked += 1
            if per_config.get(cell.config.config_hash, 0) < reverify:
                per_config[cell.config.config_hash] = per_config.get(cell.config.config_hash, 0) + 1
                verdict = await study.adapter.verify(
                    cell.task,
                    attempt,
                    VerifyContext(
                        study=study.spec.name,
                        phase="held_out",
                        attempt_dir=Path(tmp) / "a",
                        keep_artifacts=False,
                    ),
                )
                if verdict.status.value != (rec.get("verdict") or {}).get("status"):
                    ok &= _ok(f"re-verified {cell.task.id} / {cell.config.label}", False)
    ok &= _ok(
        "held-out outputs recompiled", mismatched == 0, f"{checked} cells, {mismatched} differ"
    )
    return ok


if __name__ == "__main__":
    sys.exit(main())

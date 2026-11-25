"""Freeze the task lists of the Haiku feedback study (standard library only).

* held_out: the 204 miniF2F-lean4 test problems that the 2026-11 math study never used. They are
  ranks 41-244 of the same seeded hash ranking that chose that study's 40 held-out problems
  (``sha256(f"20261122:test:{id}")``), kept in rank order. No record, plan or document in this
  repository names any of them before this study (checked when this study was designed).
* dev: the math study's 30 dev problems (miniF2F valid), already used there. Here they serve only
  the smoke test and the setup check; nothing is selected on them.
* sensitivity_exclusions: the held-out problems flagged on a public community list of miniF2F
  Lean 4 statement defects (Hugging Face, cat-searcher/minif2f-lean4, discussion 2, created
  2026-07-19). That list was made on another Lean 4 port, so a flag may not apply to this one;
  the registered sensitivity analysis drops them anyway.

Usage::

    python studies/2026-11-math-feedback-haiku/select_tasks.py <miniF2F-lean4 checkout at 5746b7d>
    python studies/2026-11-math-feedback-haiku/select_tasks.py <checkout> --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MATH_SPLIT = HERE.parent / "2026-11-math-minif2f" / "split.json"
OUT = HERE / "split.json"
REPO_URL = "https://github.com/yangky11/miniF2F-lean4"
PINNED_COMMIT = "5746b7d6c47855ce1294bed87329618ff7f1bc31"
SEED = 20261122  # the math study's ranking seed: ranks 1-40 went to its held-out set
USED = 40
DEFECT_LIST = {  # all 23 names on the community list; only those in the held-out set are kept
    "aime_1996_p5", "aime_1990_p2", "algebra_ineq_nto1onlt2m1on", "mathd_algebra_114",
    "mathd_algebra_208", "aime_1994_p3", "amc12a_2002_p21", "amc12a_2020_p10", "amc12a_2020_p13",
    "imo_1979_p1", "imo_1982_p1", "mathd_numbertheory_618", "imo_1962_p4",
    "mathd_numbertheory_126", "mathd_numbertheory_780", "imo_1974_p5", "amc12_2000_p6",
    "imo_1984_p2", "mathd_algebra_76", "mathd_numbertheory_156", "amc12a_2021_p7", "imo_1983_p6",
    "mathd_algebra_101",
}  # fmt: skip
THEOREM = re.compile(r"^theorem\s+(\S+)(.*?):=\s*by\s+sorry\s*$", re.S | re.M)


def _statement(text: str) -> tuple[str, str]:
    m = THEOREM.search(text)
    if m is None:
        raise ValueError("no `theorem ... := by sorry` found")
    return m.group(1), " ".join(m.group(2).split())


def _load(repo: Path, folder: str) -> dict[str, dict[str, str]]:
    out = {}
    for path in sorted((repo / "MiniF2F" / folder).glob("*.lean")):
        raw = path.read_bytes()
        name, body = _statement(raw.decode("utf-8"))
        if name != path.stem:
            raise ValueError(f"{path}: theorem {name!r} does not match the file name")
        out[path.stem] = {
            "file": f"MiniF2F/{folder}/{path.name}",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "statement": body,
        }
    return out


def build(repo: Path) -> dict:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != PINNED_COMMIT:
        raise SystemExit(f"{repo} is at {head}, expected the pinned commit {PINNED_COMMIT}")
    math = json.loads(MATH_SPLIT.read_text(encoding="utf-8"))
    valid, test = _load(repo, "Valid"), _load(repo, "Test")
    ranked = sorted(test, key=lambda p: hashlib.sha256(f"{SEED}:test:{p}".encode()).hexdigest())
    if ranked[:USED] != [e["id"] for e in math["held_out"]]:
        raise SystemExit("the ranking does not reproduce the math study's held-out problems")
    held = ranked[USED:]
    dev = [e["id"] for e in math["dev"]]
    for e in math["dev"]:
        if valid[e["id"]]["sha256"] != e["sha256"]:
            raise SystemExit(f"dev problem {e['id']} differs from the math study's split")
    by_stmt: dict[str, list[str]] = {}
    for pid in held:
        by_stmt.setdefault(test[pid]["statement"], []).append(pid)
    shared = sorted([d, h] for d in dev for h in by_stmt.get(valid[d]["statement"], []))
    if set(dev) & set(held) or shared:
        raise SystemExit(f"dev and held-out share ids or statements: {shared}")
    return {
        "study": "2026-11-math-feedback-haiku",
        "dataset": math["dataset"],
        "selection": {
            "held_out": (
                f"ranks {USED + 1}-{len(ranked)} (in rank order) of the math study's ranking: every "
                f"test problem ranked by sha256(f'{SEED}:test:{{id}}'); ranks 1-{USED} were its "
                "held-out problems"
            ),
            "dev": "the math study's 30 dev problems (already used there); setup checks only",
            "source_split": {
                "file": "studies/2026-11-math-minif2f/split.json",
                "file_sha256": hashlib.sha256(MATH_SPLIT.read_bytes()).hexdigest(),
            },
        },
        "dev": [{"id": i, "file": valid[i]["file"], "sha256": valid[i]["sha256"]} for i in dev],
        "held_out": [{"id": i, "file": test[i]["file"], "sha256": test[i]["sha256"]} for i in held],
        "sensitivity_exclusions": {
            "source": "https://huggingface.co/datasets/cat-searcher/minif2f-lean4/discussions/2",
            "note": "flagged on another Lean 4 port; not checked against this port",
            "ids": sorted(set(held) & DEFECT_LIST),
        },
        "checks": {"shared_ids": [], "dev_held_out_shared_statements": shared},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path, help="checkout of miniF2F-lean4 at the pinned commit")
    parser.add_argument("--check", action="store_true", help="fail if split.json would change")
    args = parser.parse_args()
    text = json.dumps(build(args.repo.resolve()), indent=2) + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            sys.exit(f"{OUT} does not match a fresh selection")
        print(f"{OUT.name} reproduced")
        return
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

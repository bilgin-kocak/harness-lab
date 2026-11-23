"""Freeze the dev and held-out task lists of the miniF2F (Lean 4) study.

Standard library only. Reads a checkout of yangky11/miniF2F-lean4 at the pinned commit and writes
``split.json``: the dev problems (from the valid split) and the held-out problems (from the test
split), each with the SHA-256 of its source file, plus leakage checks.

Selection is a seeded hash ranking, so it does not depend on the Python version: every problem is
ranked by ``sha256(f"{seed}:{split}:{problem_id}")`` and the first ``k`` are taken.

Usage::

    git clone https://github.com/yangky11/miniF2F-lean4 /tmp/miniF2F-lean4
    git -C /tmp/miniF2F-lean4 checkout 5746b7d6c47855ce1294bed87329618ff7f1bc31
    python studies/2026-11-math-minif2f/select_tasks.py /tmp/miniF2F-lean4

Run it again to check that ``split.json`` is reproduced byte for byte (``--check``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/yangky11/miniF2F-lean4"
PINNED_COMMIT = "5746b7d6c47855ce1294bed87329618ff7f1bc31"
SEED = 20261122
N_DEV = 30  # from MiniF2F/Valid
N_HELD_OUT = 40  # from MiniF2F/Test
OUT = Path(__file__).with_name("split.json")

THEOREM = re.compile(r"^theorem\s+(\S+)(.*?):=\s*by\s+sorry\s*$", re.S | re.M)


def rank_key(split: str, problem_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{split}:{problem_id}".encode()).hexdigest()


def statement(text: str) -> tuple[str, str]:
    """The theorem name and its statement with the name removed and whitespace collapsed."""
    m = THEOREM.search(text)
    if m is None:
        raise ValueError("no `theorem ... := by sorry` found")
    return m.group(1), " ".join(m.group(2).split())


def load(repo: Path, folder: str) -> dict[str, dict[str, str]]:
    problems: dict[str, dict[str, str]] = {}
    for path in sorted((repo / "MiniF2F" / folder).glob("*.lean")):
        raw = path.read_bytes()
        name, body = statement(raw.decode("utf-8"))
        if name != path.stem:
            raise ValueError(f"{path}: theorem {name!r} does not match the file name")
        problems[path.stem] = {
            "file": f"MiniF2F/{folder}/{path.name}",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "statement": body,
        }
    return problems


def build(repo: Path) -> dict:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if head != PINNED_COMMIT:
        raise SystemExit(f"{repo} is at {head}, expected the pinned commit {PINNED_COMMIT}")
    valid, test = load(repo, "Valid"), load(repo, "Test")
    dev = sorted(valid, key=lambda p: rank_key("valid", p))[:N_DEV]
    held = sorted(test, key=lambda p: rank_key("test", p))[:N_HELD_OUT]

    # Leakage checks: no shared ids, and no statement shared between any dev and any held-out
    # problem (names removed, whitespace collapsed). Cross-split duplicates in the whole dataset are
    # listed too, so a reader can see what the selection had to avoid.
    test_by_stmt: dict[str, list[str]] = {}
    for pid, p in test.items():
        test_by_stmt.setdefault(p["statement"], []).append(pid)
    dataset_dupes = sorted(
        [vid, tid] for vid, v in valid.items() for tid in test_by_stmt.get(v["statement"], [])
    )
    dev_held_dupes = [pair for pair in dataset_dupes if pair[0] in dev and pair[1] in held]
    if set(dev) & set(held):
        raise SystemExit(f"dev and held-out share ids: {sorted(set(dev) & set(held))}")
    if dev_held_dupes:
        raise SystemExit(f"dev and held-out share statements: {dev_held_dupes}")

    def entries(ids: list[str], pool: dict[str, dict[str, str]]) -> list[dict[str, str]]:
        return [{"id": i, "file": pool[i]["file"], "sha256": pool[i]["sha256"]} for i in ids]

    return {
        "study": "2026-11-math-minif2f",
        "dataset": {
            "repo": REPO_URL,
            "commit": PINNED_COMMIT,
            "license": "MIT (Copyright (c) Meta Platforms, Inc. and affiliates)",
            "lean_toolchain": "leanprover/lean4:v4.24.0",
            "mathlib": "v4.24.0 (f897ebcf72cd16f89ab4577d0c826cd14afaafc7)",
            "valid_size": len(valid),
            "test_size": len(test),
        },
        "selection": {
            "seed": SEED,
            "method": "rank every problem by sha256(f'{seed}:{split}:{id}') and take the first k",
            "dev": {"split": "valid", "k": N_DEV},
            "held_out": {"split": "test", "k": N_HELD_OUT},
        },
        "dev": entries(dev, valid),
        "held_out": entries(held, test),
        "checks": {
            "shared_ids": [],
            "dev_held_out_shared_statements": dev_held_dupes,
            "valid_test_shared_statements_in_dataset": dataset_dupes,
        },
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

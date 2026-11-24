"""Write the pilot's split files from the 2026-11 math study's split (standard library only).

The pilot runs on the 40 problems the math study used as its held-out set. They were used once
there, so they are no longer held out from anything: here they are the pilot's ``dev`` problems.
The protocol needs a non-empty held-out side, so the math study's 30 dev problems fill it; the
pilot never runs them (it has no freeze and no held-out phase). The 204 miniF2F test problems the
math study never used are not in either list, so the adapter cannot load them.

Usage::

    python studies/2026-11-math-pilot/make_split.py          # write haiku/ and opus/split.json
    python studies/2026-11-math-pilot/make_split.py --check  # check they are reproduced byte for byte
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "2026-11-math-minif2f" / "split.json"
MODELS = ("haiku", "opus")


def build(model: str) -> bytes:
    raw = SOURCE.read_bytes()
    src = json.loads(raw)
    data = {
        "study": f"2026-11-math-pilot-{model}",
        "purpose": (
            "Exploratory pilot. dev: the math study's 40 held-out problems (used once there). "
            "held_out: the math study's 30 dev problems, listed only because the protocol needs a "
            "held-out side; the pilot never runs them."
        ),
        "source_split": {
            "file": "studies/2026-11-math-minif2f/split.json",
            "file_sha256": hashlib.sha256(raw).hexdigest(),
        },
        "dataset": src["dataset"],
        "dev": src["held_out"],
        "held_out": src["dev"],
        "checks": src["checks"],
    }
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ok = True
    for model in MODELS:
        out = HERE / model / "split.json"
        data = build(model)
        if args.check:
            same = out.is_file() and out.read_bytes() == data
            print(f"{out.relative_to(HERE)}: {'reproduced' if same else 'DIFFERS'}")
            ok &= same
        else:
            out.write_bytes(data)
            print(f"wrote {out.relative_to(HERE)} ({len(data)} bytes)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Assemble the full paper draft: PAPER.md with Appendix A's documents inlined verbatim.

Appendix A reproduces the registration exactly as registered (`REGISTRATION.md` at commit
64abcd7, read from git, its SHA-256 checked against the registered value) and the deviation
record (`DEVIATIONS.md` as it stands). Their headings are demoted so they nest under Appendix A;
nothing else in them changes.

Usage (repository root; pandoc optional)::

    python studies/2026-11-math-feedback-haiku/paper/build.py

Writes `build/PAPER_full.md` and, if pandoc is installed, `build/PAPER_full.html` (standalone,
figures embedded) next to this script.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = STUDY.parents[1]
REG_COMMIT = "64abcd7d920bb8f702f42db50a726fd8b899943d"
REG_SHA256 = "6a9a3ed682b77ad4861ec2c453e2ff044c022b6de6802508d157a058cbd754b0"
MARKER = "## Appendix B."


def demote(text: str, levels: int = 3) -> str:
    out, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and re.match(r"^#{1,6} ", line):
            line = "#" * levels + line
        out.append(line)
    return "\n".join(out) + "\n"


def main() -> int:
    paper = (HERE / "PAPER.md").read_text(encoding="utf-8")
    reg = subprocess.run(
        ["git", "show", f"{REG_COMMIT}:studies/2026-11-math-feedback-haiku/REGISTRATION.md"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout
    digest = hashlib.sha256(reg).hexdigest()
    if digest != REG_SHA256:
        raise SystemExit(f"REGISTRATION.md at {REG_COMMIT[:7]} has SHA-256 {digest}, not the registered one")
    dev = (STUDY / "DEVIATIONS.md").read_text(encoding="utf-8")
    inlined = (
        "### A.1 `REGISTRATION.md` at commit `64abcd7` (verbatim)\n\n"
        + demote(reg.decode("utf-8"))
        + "\n### A.2 `DEVIATIONS.md` (verbatim)\n\n"
        + demote(dev)
        + "\n"
    )
    if paper.count(MARKER) != 1:
        raise SystemExit(f"expected one '{MARKER}' heading in PAPER.md")
    full = paper.replace(MARKER, inlined + MARKER)
    out = HERE / "build"
    out.mkdir(exist_ok=True)
    (out / "PAPER_full.md").write_text(full, encoding="utf-8")
    print(f"wrote {out / 'PAPER_full.md'} (registration SHA-256 checked)")
    pandoc = shutil.which("pandoc")
    if pandoc:
        subprocess.run(
            [pandoc, "PAPER_full.md", "-f", "gfm", "-t", "html5", "--standalone", "--embed-resources",
             "--resource-path", str(HERE), "--metadata", "title=Paper draft (not for circulation)",
             "-o", "PAPER_full.html"],
            cwd=out, check=True,
        )
        print(f"wrote {out / 'PAPER_full.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

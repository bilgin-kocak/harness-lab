"""Assemble the full paper draft: PAPER.md with Appendix A's documents inlined verbatim.

Appendix A reproduces the registration exactly as registered (`REGISTRATION.md` at commit
64abcd7, read from git, its SHA-256 checked against the registered value) and the deviation
record (`DEVIATIONS.md` as it stands). Their headings are demoted so they nest under Appendix A;
nothing else in them changes.

Usage (repository root; pandoc optional)::

    python studies/2026-11-math-feedback-haiku/paper/build.py

Writes `build/PAPER_full.md` and, if pandoc is installed, `build/PAPER_full.html` (standalone,
figures embedded) next to this script; if node and playwright are also available, it prints that
page to `build/PAPER_full.pdf` (A4, page numbers, a "draft, not for circulation" footer).
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
        (out / "style.html").write_text(STYLE, encoding="utf-8")
        subprocess.run(
            [pandoc, "PAPER_full.md", "-f", "gfm", "-t", "html5", "--standalone", "--embed-resources",
             "--resource-path", str(HERE), "--include-in-header", "style.html",
             "--metadata", "pagetitle=Paper draft (not for circulation)", "-o", "PAPER_full.html"],
            cwd=out, check=True,
        )
        print(f"wrote {out / 'PAPER_full.html'}")
        node = shutil.which("node")
        if node:
            (out / "print_pdf.js").write_text(PRINT_JS, encoding="utf-8")
            done = subprocess.run([node, "print_pdf.js"], cwd=out, capture_output=True, text=True)
            if done.returncode == 0:
                print(f"wrote {out / 'PAPER_full.pdf'}")
            else:
                print(f"no PDF (playwright unavailable?): {done.stderr.strip()[-300:]}")
    return 0


STYLE = """<style>
body{max-width:68em;font-size:15px;line-height:1.45}
table{font-size:13px;border-collapse:collapse}
th,td{padding:3px 6px;vertical-align:top}
img{max-width:100%;height:auto}
code{font-size:90%}
@media print{
  body{max-width:none;margin:0;padding:0;font-family:'Liberation Serif','DejaVu Serif',serif;
       font-size:10.5pt;line-height:1.35;color:#000}
  h1{font-size:17pt;line-height:1.2;margin:0 0 .6em}
  h2{font-size:13pt;margin:1.1em 0 .4em;break-after:avoid}
  h3,h4,h5,h6{font-size:11pt;margin:.9em 0 .3em;break-after:avoid}
  p{margin:.45em 0;orphans:3;widows:3}
  table{font-size:8.3pt;margin:.6em 0;break-inside:auto}
  tr,img,figure{break-inside:avoid}
  th,td{padding:2px 4px;border-bottom:.5px solid #bbb}
  code{font-family:'DejaVu Sans Mono',monospace;font-size:8.6pt}
  table code{font-size:7.4pt}
  pre{white-space:pre-wrap;word-break:break-word;font-size:7.8pt;line-height:1.3;
      background:#f6f6f6;padding:6px;border-radius:3px}
  pre code{font-size:inherit}
  blockquote{margin:.5em 0;padding:.3em .8em;border-left:3px solid #999;color:#333;font-size:9.5pt}
  a{color:#000;text-decoration:none}
  hr{border:none;border-top:.5px solid #999;margin:1.2em 0}
}
</style>
"""

PRINT_JS = """const { chromium } = require('playwright');
const path = require('path');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + path.resolve('PAPER_full.html'), { waitUntil: 'load' });
  await page.emulateMedia({ media: 'print' });
  await page.pdf({
    path: 'PAPER_full.pdf', format: 'A4', printBackground: true,
    margin: { top: '18mm', bottom: '18mm', left: '18mm', right: '18mm' },
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    footerTemplate: '<div style="font-size:8px;width:100%;text-align:center;color:#555;font-family:serif">' +
      'Draft for the owner, not for circulation &middot; page <span class="pageNumber"></span> of ' +
      '<span class="totalPages"></span></div>',
  });
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


if __name__ == "__main__":
    sys.exit(main())

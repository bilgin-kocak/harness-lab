"""Make the paper's figures from the study's data (standard library only; writes SVG).

Figure 1 and Figure 2 read `analysis.json` (the registered output) and, for the pilot and the
earlier math study, the numbers printed in their reports (`../../2026-11-math-pilot/REPORT.md`,
`../../../docs/results/2026-11-23-math.md`), copied below with their source line. Figure 3 is
EXPLORATORY (not registered): it is computed from `held_out/attempts.jsonl.gz`.

Usage (repository root)::

    python studies/2026-11-math-feedback-haiku/paper/make_figures.py

Writes `figures/fig1_contrasts.svg`, `figures/fig2_pilot_and_earlier.svg` and
`figures/fig3_rescue_by_call.svg` next to this script, and prints the numbers each figure shows.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
OUT = HERE / "figures"

FONT = "Inter, 'DejaVu Sans', Helvetica, Arial, sans-serif"
TEXT = "#191919"
TEXT2 = "#666666"
GRID = "#e6e6e6"
BAND = "#efefef"
SURFACE = "#ffffff"
SERIES = ["#257de0", "#f47230", "#00b894"]  # validated categorical slots 1-3 (light)
PAST = "#8c8c8c"  # earlier and pilot estimates (neutral ink)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Svg:
    def __init__(self, w: int, h: int, title: str, desc: str):
        self.w, self.h = w, h
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" font-family="{FONT}">',
            f"<title>{esc(title)}</title><desc>{esc(desc)}</desc>",
            f'<rect width="{w}" height="{h}" fill="{SURFACE}"/>',
        ]

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, size=12, color=TEXT, anchor="start", weight="normal") -> None:
        self.add(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{esc(s)}</text>'
        )

    def line(self, x1, y1, x2, y2, color=TEXT, width=1.0, cap="butt", dash=None) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="{cap}"{d}/>'
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.parts + ["</svg>"]) + "\n", encoding="utf-8")


def fmt(v: float, digits: int = 1) -> str:
    s = f"{v:+.{digits}f}".replace("-", "−")
    return "0.0" if s in ("+0.0", "−0.0") else s


def forest(svg: Svg, x0, y0, w, rows, lo, hi, step, title, band=None, label_w=150, note=None):
    """rows: dicts with label, est, ci95, optional ci90, color, sub. Values in points."""
    plot_x0 = x0 + label_w
    plot_w = w - label_w - 10
    row_h = 34
    top = y0 + 44
    plot_h = row_h * len(rows)

    def X(v):
        return plot_x0 + (v - lo) / (hi - lo) * plot_w

    svg.text(x0, y0 + 14, title, size=13, weight="600")
    if band is not None:
        svg.add(
            f'<rect x="{X(-band):.1f}" y="{top - 8:.1f}" width="{X(band) - X(-band):.1f}" '
            f'height="{plot_h + 8:.1f}" fill="{BAND}"/>'
        )
        svg.text(X(0), top - 13, f"±{band}-point margin", size=10.5, color=TEXT2, anchor="middle")
    v = lo
    while v <= hi + 1e-9:
        svg.line(X(v), top - 8, X(v), top + plot_h, GRID if abs(v) > 1e-9 else TEXT2, 1)
        svg.text(X(v), top + plot_h + 16, fmt(v, 0) if v else "0", size=10.5, color=TEXT2, anchor="middle")
        v += step
    svg.text(plot_x0 + plot_w / 2, top + plot_h + 33, "difference in verified rate (points)",
             size=11, color=TEXT2, anchor="middle")
    for i, r in enumerate(rows):
        cy = top + row_h * i + row_h / 2
        svg.text(x0, cy - 1, r["label"], size=12, color=TEXT, weight="600" if r.get("bold") else "normal")
        if r.get("sub"):
            svg.text(x0, cy + 12, r["sub"], size=10, color=TEXT2)
        c = r["color"]
        a, b = r["ci95"]
        svg.line(X(a), cy, X(b), cy, c, 2, cap="round")
        if r.get("ci90"):
            a9, b9 = r["ci90"]
            svg.line(X(a9), cy, X(b9), cy, c, 6, cap="round")
        svg.add(
            f'<circle cx="{X(r["est"]):.1f}" cy="{cy:.1f}" r="5" fill="{c}" '
            f'stroke="{SURFACE}" stroke-width="2"/>'
        )
        svg.add(
            f'<text x="{X(r["est"]):.1f}" y="{cy - 10:.1f}" font-size="10.5" fill="{TEXT}" '
            f'text-anchor="middle" paint-order="stroke" stroke="{SURFACE}" stroke-width="3" '
            f'stroke-linejoin="round">{esc(fmt(r["est"]))}</text>'
        )
    if note:
        svg.text(x0, top + plot_h + 52, note, size=10, color=TEXT2)
    return top + plot_h + 60


def pts(x):
    return [100 * v for v in x]


def figure1(a: dict) -> dict:
    p = a["primary"]
    h2 = a["secondary"]["H2 F - P"]
    h3 = a["secondary"]["H3 P - R4"]
    vb = {d["contrast"]: d for d in a["descriptive"]["vs_baseline"]}
    rows_a = [
        dict(label="H1  F − R4", sub="primary; 90% (thick) and 95%", est=100 * p["estimate"],
             ci95=pts(p["ci95"]), ci90=pts(p["ci90"]), color=SERIES[0], bold=True),
        dict(label="H2  F − P", sub="secondary; 95%", est=100 * h2["estimate"],
             ci95=pts(h2["ci95"]), color=SERIES[0]),
        dict(label="H3  P − R4", sub="secondary; 95%", est=100 * h3["estimate"],
             ci95=pts(h3["ci95"]), color=SERIES[0]),
    ]
    rows_b = [
        dict(label=name.replace(" - baseline", " − B"), sub="95%", est=100 * vb[name]["estimate"],
             ci95=pts(vb[name]["ci95"]), color=TEXT2)
        for name in ("R4 - baseline", "F - baseline", "P - baseline")
    ]
    svg = Svg(900, 230, "Figure 1. Registered contrasts and contrasts with one call",
              "Left: H1-H3 with the 3-point margin. Right: descriptive contrasts with B.")
    forest(svg, 10, 6, 470, rows_a, -4, 4, 1, "(a) Registered contrasts (H1–H3)", band=3,
           note="Readings: no difference of 3 points or more (all three).")
    forest(svg, 500, 6, 390, rows_b, 0, 14, 2, "(b) Against one call (descriptive)", label_w=110,
           note="Arms capped at 4 calls against B (1 call).")
    svg.save(OUT / "fig1_contrasts.svg")
    return {"a": rows_a, "b": rows_b}


def figure2(a: dict) -> dict:
    p = a["primary"]
    vb = {d["contrast"]: d for d in a["descriptive"]["vs_baseline"]}
    # Pilot: 2026-11-math-pilot/REPORT.md, "Contrasts" table (Haiku and Opus columns).
    # Earlier study: docs/results/2026-11-23-math.md, "frozen vs baseline" row (+0.125 [+0.0417, +0.225]).
    rows_a = [
        dict(label="Pilot, Haiku 5.5", sub="40 reused problems × 3; arms in separate blocks",
             est=5.8, ci95=[0.8, 12.5], color=PAST),
        dict(label="Pilot, Opus 5.5", sub="40 × 3; F and R4 run a day apart",
             est=5.8, ci95=[0.0, 12.5], color=PAST),
        dict(label="This study, Haiku 5.5", sub="204 new problems × 5; interleaved",
             est=100 * p["estimate"], ci95=pts(p["ci95"]), color=SERIES[0], bold=True),
    ]
    rows_b = [
        dict(label="Earlier study, Opus: F − B", sub="40 × 3", est=12.5, ci95=[4.2, 22.5], color=PAST),
        dict(label="Pilot, Opus: R4 − B", sub="40 × 3; a day apart", est=6.7, ci95=[0.8, 14.2], color=PAST),
        dict(label="Pilot, Haiku: F − B", sub="40 × 3", est=13.3, ci95=[5.8, 21.7], color=PAST),
        dict(label="Pilot, Haiku: R4 − B", sub="40 × 3", est=7.5, ci95=[2.5, 14.2], color=PAST),
    ] + [
        dict(label=f"This study: {n.split(' - ')[0]} − B", sub="204 × 5", est=100 * vb[n]["estimate"],
             ci95=pts(vb[n]["ci95"]), color=SERIES[0], bold=True)
        for n in ("F - baseline", "R4 - baseline", "P - baseline")
    ]
    svg = Svg(940, 360, "Figure 2. The pilot and the earlier study against this study",
              "Estimates with 95% bootstrap intervals. No p-values, no pooled estimate.")
    forest(svg, 10, 6, 470, rows_a, -2, 14, 2, "(a) F − R4", label_w=215)
    forest(svg, 490, 6, 445, rows_b, 0, 24, 4, "(b) Gain over one call (B)", label_w=190)
    svg.save(OUT / "fig2_pilot_and_earlier.svg")
    return {"a": rows_a, "b": rows_b}


def figure3() -> dict:
    """EXPLORATORY (not registered): attempts whose first call failed in the loop, verified at call k."""
    arms = ("F", "R4", "P")
    failed, at = Counter(), {k: Counter() for k in arms}
    with gzip.open(STUDY / "held_out" / "attempts.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            arm = r["config_label"]
            if arm not in arms:
                continue
            rounds = (r["attempt"].get("details") or {}).get("rounds") or []
            if rounds and (rounds[0].get("inloop") or {}).get("status") == "pass":
                continue
            failed[arm] += 1
            if (r.get("verdict") or {}).get("status") == "pass":
                at[arm][len(rounds)] += 1
    calls = (2, 3, 4)
    data = {k: {c: at[k][c] / failed[k] for c in calls} for k in arms}
    w, h = 620, 300
    svg = Svg(w, h, "Figure 3. Attempts whose first call failed: share verified at each later call",
              "Exploratory (not registered). Grouped bars by call number for F, R4 and P.")
    x0, top, ph, pw = 60, 64, 170, 530
    ymax = 0.6

    def Y(v):
        return top + ph - v / ymax * ph

    svg.text(10, 20, "Attempts whose first call failed: share verified at call 2, 3 or 4", size=13, weight="600")
    svg.text(10, 37, f"Exploratory (not registered). Failed first calls: F {failed['F']}, "
             f"R4 {failed['R4']}, P {failed['P']}.", size=10.5, color=TEXT2)
    for v in (0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        svg.line(x0, Y(v), x0 + pw, Y(v), GRID if v else TEXT2, 1)
        svg.text(x0 - 8, Y(v) + 4, f"{round(v * 100)}%", size=10.5, color=TEXT2, anchor="end")
    group_w = pw / len(calls)
    bw, gap = 24, 2
    for gi, c in enumerate(calls):
        gx = x0 + group_w * gi + group_w / 2
        total = len(arms) * bw + (len(arms) - 1) * gap
        for ai, arm in enumerate(arms):
            v = data[arm][c]
            bx = gx - total / 2 + ai * (bw + gap)
            y = Y(v)
            hgt = Y(0) - y
            rr = min(4, hgt / 2)
            # rounded data-end at the top, square at the baseline
            svg.add(
                f'<path d="M{bx:.1f},{Y(0):.1f} V{y + rr:.1f} Q{bx:.1f},{y:.1f} {bx + rr:.1f},{y:.1f} '
                f'H{bx + bw - rr:.1f} Q{bx + bw:.1f},{y:.1f} {bx + bw:.1f},{y + rr:.1f} V{Y(0):.1f} Z" '
                f'fill="{SERIES[ai]}"/>'
            )
            svg.text(bx + bw / 2, y - 5, f"{at[arm][c]}", size=10, color=TEXT, anchor="middle")
        svg.text(gx, Y(0) + 18, f"call {c}", size=11.5, color=TEXT, anchor="middle")
    lx = x0 + pw - 170
    for ai, arm in enumerate(arms):
        svg.add(f'<rect x="{lx + ai * 58:.1f}" y="50" width="12" height="12" rx="2" fill="{SERIES[ai]}"/>')
        svg.text(lx + ai * 58 + 17, 60, arm, size=11.5)
    svg.text(10, Y(0) + 42, "Numbers on the bars: attempts verified at that call. Cumulative after call 4: "
             f"F {sum(at['F'].values())}/{failed['F']}, R4 {sum(at['R4'].values())}/{failed['R4']}, "
             f"P {sum(at['P'].values())}/{failed['P']}.", size=10, color=TEXT2)
    svg.save(OUT / "fig3_rescue_by_call.svg")
    return {"failed": dict(failed), "verified_at_call": {k: dict(at[k]) for k in arms}}


def main() -> int:
    a = json.loads((STUDY / "analysis.json").read_text(encoding="utf-8"))
    out = {"figure1": figure1(a), "figure2": figure2(a), "figure3_exploratory": figure3()}
    print(json.dumps(out, indent=1, default=lambda v: round(v, 3)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

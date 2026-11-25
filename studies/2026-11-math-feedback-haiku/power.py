"""Power of this study's primary test, as REGISTRATION.md section 7 reports it.

A synthetic simulation: it calls no API and uses only the pilot's committed Haiku records.

Usage: python studies/2026-11-math-feedback-haiku/power.py [studies per row, default 4000]

* **The test:** two-sided paired sign-flip test on per-problem rate differences
  d = rate_F - rate_R4, over 204 problems, at alpha 0.05.
* **How p is computed:** the null distribution of sum(d) is approximated by a normal with variance
  sum(d^2). As a check, 200 studies per row are also tested with 20,000 random sign flips (the
  'flips' column).
* **Per-problem truths** come from the pilot's 40 Haiku (R4 count, F count) pairs, in one of two
  ways:
  * plug-in (p = k/3);
  * empirical-Bayes draws from a beta-binomial prior fitted to the pilot's 80 counts.
* **scale** multiplies each problem's F - R4 gap. 1 is the pilot's effect.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from pathlib import Path

import numpy as np

REC = Path(__file__).resolve().parent.parent / "2026-11-math-pilot/haiku/dev/attempts.jsonl.gz"
N = 204


def _log_beta(x: float, y: float) -> float:
    return math.lgamma(x) + math.lgamma(y) - math.lgamma(x + y)


def fit_beta_binomial(ks: list[int], n: int = 3) -> tuple[float, float]:
    grid = [0.02 * i for i in range(1, 151)]

    def loglik(a: float, b: float) -> float:
        return sum(
            math.log(math.comb(n, k)) + _log_beta(k + a, n - k + b) - _log_beta(a, b) for k in ks
        )

    return max(((a, b) for a in grid for b in grid), key=lambda ab: loglik(*ab))


def main() -> None:
    sims = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    outcomes: dict[tuple[str, str], list[bool]] = {}
    with gzip.open(REC, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            key = (r["config_label"], r["task_id"])
            outcomes.setdefault(key, []).append(bool(r["metrics"]["verified"]))
    tasks = sorted({t for _, t in outcomes})
    pairs = np.array([(sum(outcomes[("R4", t)]), sum(outcomes[("F", t)])) for t in tasks])
    a, b = fit_beta_binomial([int(k) for k in pairs.ravel()])
    rng = np.random.default_rng(20261125)
    erfc = np.vectorize(lambda x: math.erfc(x / math.sqrt(2)))
    gap = (pairs[:, 1] - pairs[:, 0]).sum() / 120 * 100
    print(f"pilot Haiku F-R4 {gap:+.1f} points; prior Beta({a:.2f}, {b:.2f}); {sims} studies/row")
    print(
        f"{'scenario':26s}{'reps':>5}{'true effect':>12}{'informative':>12}{'power':>7}{'flips':>7}"
    )
    rows = (
        ("plugin", 1.0, "plug-in"),
        ("eb", 1.0, "smoothed, pilot effect"),
        ("eb", 0.75, "smoothed, 3/4 effect"),
        ("eb", 0.5, "smoothed, half effect"),
        ("eb", 0.0, "smoothed, no effect"),
    )
    for mode, scale, label in rows:
        for reps in (3, 5):
            idx = rng.integers(0, len(pairs), size=(sims, N))
            kr, kf = pairs[idx, 0], pairs[idx, 1]
            if mode == "plugin":
                pr, pf = kr / 3, kf / 3
            else:
                pr, pf = rng.beta(a + kr, b + 3 - kr), rng.beta(a + kf, b + 3 - kf)
            pf = np.clip(pr + scale * (pf - pr), 0, 1)
            d = (rng.binomial(reps, pf) - rng.binomial(reps, pr)) / reps
            s, v = d.sum(1), (d**2).sum(1)
            z = np.where(v > 0, np.abs(s) / np.sqrt(np.where(v > 0, v, 1)), 0.0)
            power = (np.where(v > 0, erfc(z), 1.0) < 0.05).mean()
            exact = []
            for i in range(200):
                flips = rng.choice([-1.0, 1.0], size=(20000, N))
                hits = (np.abs(flips @ d[i]) >= abs(d[i].sum()) - 1e-12).sum()
                exact.append((1 + hits) / 20001 < 0.05)
            print(
                f"{label:26s}{reps:>5}{(pf - pr).mean() * 100:>+11.1f}p"
                f"{(d != 0).sum(1).mean():>12.1f}{power:>7.3f}{np.mean(exact):>7.3f}"
            )


if __name__ == "__main__":
    main()

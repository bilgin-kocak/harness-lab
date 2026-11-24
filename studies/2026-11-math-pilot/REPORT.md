# Pilot report (draft, exploratory, not for quoting)

**Status: draft.** This is not a result to publish or quote, and the owner has approved no claim in
it. The pilot used 40 miniF2F problems that are public since 2021 and that the 2026-11 math study had
already used once. Each configuration ran 3 repetitions. It says nothing about new problems.

**Run.** Code: commit `a17d9a1` and later (see `RUNLOG.md`). Spend:

| Part | Attempts | Cost |
|---|---|---|
| Haiku pilot stage | 360 | $1.18 |
| Opus pilot stage | 120 | $15.14 |
| Pilot stage, both models | 480 | $16.32 |
| Smoke tests | 12 | $0.33 |
| **Total** | | **$16.65** |

The projection was about $17 for the pilot stage, and $23–26 if it went badly.

- Every call was served by the configured model.
- There were no refusals and no harness errors, and every attempt completed.

## Numbers (`results.json`, `analysis.txt`)

**Per-problem verified rates** (verified attempts / 120):

| Configuration | Haiku 5.5 | Opus 5.5 |
|---|---|---|
| baseline (1 call) | 80.8% (97) | 85.0% (102, math study, 2026-11-23) |
| R4 (up to 4 fresh tries) | 88.3% (106) | 91.7% (110, pilot, 2026-11-24) |
| F (feedback, up to 4 calls) | 94.2% (113) | 97.5% (117, math study, 2026-11-23) |

**Contrasts.** Each row gives the difference, its 95% bootstrap interval, the problems up / down /
tied, and the exact sign test.

| Contrast | Haiku | Opus |
|---|---|---|
| R4 − baseline | +7.5 [+2.5, +14.2], 6/0/34, p = 0.031 | +6.7 [+0.8, +14.2], 6/1/33, p = 0.125 |
| F − R4 | +5.8 [+0.8, +12.5], 4/0/36, p = 0.125 | +5.8 [0.0, +12.5], 3/0/37, p = 0.25 |
| F − baseline | +13.3 [+5.8, +21.7], 10/0/30, p = 0.002 | +12.5 [+4.2, +22.5], 8/0/32, p = 0.008 |

## How to read them

- **F − R4 is not significant in either model.** Only 3 and 4 problems differed, and p = 0.25 and
  0.125 are the smallest p-values the sign test can give with 3 and 4 problems, so this pilot could
  not have shown significance for this contrast.
  - 26 of the 40 problems were proved in every attempt by every configuration, and one
    (`imosl_2007_algebra_p6`) in none. Only 13 problems carry any information.
  - The same two problems (`aime_1991_p9`, `amc12a_2013_p4`) make up about 71% of the F − R4 sum in
    each model, so the two models are not independent confirmations.
  - Haiku's F win on `amc12_2000_p6` came from first-call successes, made before any feedback.
    Counted as a tie, as the math study did in such a case, Haiku's F − R4 is 3/0, p = 0.25.
- **The Opus split is rough, not an estimate to quote.** Of the math study's +12.5 points, the point
  estimates put +6.7 on R4 − baseline and +5.8 on F − R4.
  - Both parts compare runs made a day apart.
  - The intervals are too wide to say how the gain divides.
- **Noise between runs is as large as the effects.** Every configuration sends the same first prompt.
  Yet Haiku's first-call success was 103 (F), 99 (R4) and 97 (baseline) of 120. The Haiku
  configurations ran one after another (baseline 18:02–18:23Z, F 18:23–18:48Z, R4 18:48–19:15Z). A
  real study should interleave the arms in time.
- **A post-hoc descriptive worth pre-registering next time.** It measures how often a failed first
  call was rescued later in the attempt. This was not in PLAN.md.

  | | F | R4 |
  |---|---|---|
  | Opus | 18/21 | 10/20 |
  | Haiku | 10/17 | 7/21 |

- **Defect-list problems (post-hoc).** `amc12_2000_p6` and `mathd_numbertheory_618` are on a public
  defect list.
  - Excluding them moves each estimate by 0.003–0.011, with no sign change.
  - It weakens some tests: Haiku R4 − baseline goes to p = 0.0625, and the lower bounds of Opus
    R4 − baseline and Haiku F − R4 go to 0.000.
- **Timing-sensitive verdicts.** Four recorded failures are 300 s verifier timeouts, not Lean errors.
  They are on `algebra_abpbcpcageq3_sumaonsqrtapbgeq3onsqrt2`: Haiku baseline reps 0 and 2, and Opus
  R4 reps 0 and 2. They timed out again on re-check. They decide some per-problem rates.

## Cost

**Cost per verified proof** (list prices; `price_check.md`):

| Configuration | Haiku | Opus |
|---|---|---|
| baseline | $0.0024 | $0.064 |
| F | $0.0039 | $0.113 |
| R4 | $0.0047 | $0.138 |

- **F cost about 18% less per verified proof than R4 in both models** on these 40 problems.
  - The gap comes from the few problems where R4 used all 4 tries. F was cheaper on only 17 of the
    40.
  - A bootstrap over problems gives F/R4 ratios of [0.59, 0.99] for Opus and [0.65, 0.98] for
    Haiku.
- **Haiku F cost about 1/29 as much per proof as Opus F.** Most of that ratio comes from one problem
  neither model proved (`imosl_2007_algebra_p6`), which is 59% of Opus F's cost.
  - Without that problem the ratio is about 1/14. Over resampled problems it ranges from 1/11 to
    1/69.
  - Haiku failed `imo_1997_p5` in every attempt, where Opus F proved it 3/3.
  - This comparison was not planned, and the two runs were a day apart.

## Checks of these numbers (2026-11-24)

1. **Independent recomputation.** A standard-library script recomputed everything from the raw
   records, without the harness code. Every value matches: rates, differences, wins, losses, ties,
   sign tests, costs and token-cap stops.
   - Bootstrap interval endpoints agree within one 1/120 step, which is Monte Carlo noise.
   - Integrity checks pass:
     - every configuration has exactly 40 problems × 3 records;
     - there are no duplicates;
     - `verified` equals the verdict in every record;
     - every call's cost equals its tokens times the listed prices.
2. **Lean re-check.** The verifier ran again, in fresh Lean processes and with no model client, on
   all 234 records of the 13 problems that differ in any contrast (pilot and math study).
   - 234 of 234 verdicts match the records.
   - The same candidate files were compiled.
3. **Known minor issue (not fixed).** `harnesslab.study.stats.bootstrap_mean_diff` reports
   `p_positive` for "Opus: R4 − baseline" as 0.9927; the exact value is 0.9833.
   - The cause: resample means that are exactly zero come out as about +5.6e-17 and are counted as
     positive.
   - It affects only contrasts with a negative difference, and `p_positive` is not used in
     `analysis.txt`.
   - Fixing it in the core would change the stored `p_positive` of earlier studies, so it is left
     for a separate change.

## For a later study (planning only)

- **Haiku cost.** At the pilot's measured Haiku costs, with a placebo arm priced like F: 4 arms × 5
  repetitions × the 204 unused problems is 4,080 attempts, about $14. The range is $9–19, depending
  on how many very hard problems the 204 contain. Smoke tests and reruns come on top.
- **Opus cost.** The same design on Opus would cost about $410.
- **Power for F − R4 on 204 problems.** This is a two-sided sign test at α 0.05, from a simulation
  resampling the pilot's per-problem patterns.
  - About 0.8–0.9 if the true effect is the pilot's (about +5.5 points).
  - About 0.5–0.65 if it is half that.
  - Going from 3 to 5 repetitions changes little. More problems, or problems that are neither
    trivial nor impossible, change a lot.

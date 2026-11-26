# Results: does showing a failed proof with Lean's report beat fresh tries? (Claude Haiku 5.5, miniF2F)

- **Status: draft for the owner's review, 2026-11-26. Not published.** No claim from this file may
  be made public until the owner approves it.
- **Registration:** `REGISTRATION.md` (commit 64abcd7, 2026-11-25), before any paid call.
- **Registered result:** `analysis.json` and `analysis_stdout.txt`, the output of `analyze.py`
  run unchanged. Every file of section 11 matches its registered SHA-256.
- **Exploratory (not registered):** everything marked so was added after the data were seen. It
  changes no registered reading (section 6.8).

## 1. Bottom line

- **Primary (H1).** With the same cap of 4 calls, the registered test found no difference between
  showing the model its failed proof with Lean's report (F) and fresh tries (R4), and the 90%
  interval excludes a difference of 3 points or more in either direction.
  - F verified 93.4% of attempts, R4 92.1%: **+1.4 points** (90% interval +0.2 to +2.6).
  - Two-sided sign-flip p = 0.093 (8 problems higher with F, 4 higher with R4, 192 equal).
  - The registered reading is **"no difference of 3 points or more in the overall verified rate"**.
    It does not say the difference is zero, and the estimate is positive.
- **Secondary (H2, H3).** F against P (Lean's report replaced by "Lean did not accept this proof.")
  and P against R4 also read "no difference of 3 points or more": F − P +1.4 (Holm p = 0.12),
  P − R4 0.0 (p = 1). Nothing is attributed to Lean's report (6.3).
- **Against one call (descriptive).** Every arm with a cap of 4 calls (1.33-1.36 made on average)
  verified 8-10 points more than the single-call baseline (B 83.8%; R4 and P 92.1%; F 93.4%).
- **The pilot's gap did not hold up.** The exploratory pilot (40 problems) had F 5.8 points above
  R4, and this study was powered for +4.1. On 204 fresh problems the estimate is +1.4.
- **The memory problem (section 7).** Proof checks of one problem (`mathd_numbertheory_451`) used
  over 13 GB, and the kernel killed 17 of its 20 recorded final checks, in all four arms. The same
  memory events also ended two checks on other problems whose proofs pass when re-checked alone:
  one in F (`aime_1990_p15`), one in B.
  - The registered rule decides, and it was fixed before the outcomes were seen: an attempt counts
    as verified only when the verifier accepts its proof (5.4), and D1, committed before any
    outcome by arm was looked at, kept killed checks "not verified". Under it every reading above
    stands, whatever later re-checks show.
  - Exploratory (not registered): counting the two artifacts as passes changes no reading (H1
    +1.5 points, p = 0.062). H1 would read "difference (positive)" only if, besides them, one of
    F's 5 killed proofs of `mathd_numbertheory_451` also passed (p = 0.043), or three of them
    without the artifacts (p = 0.048).
  - Very likely none of F's 5 could have passed within the registered 300 s (exploratory).
    Re-checked as the only Lean process (light session commands were also running), the fifth ran
    its full 300 s and timed out. With the memory limit lifted by swap, each of the other 4 computed
    for more than 330 s of user CPU without finishing, its memory still growing past 21 GB. Unless
    memory pressure added about 10% to that user time, or the run's CPU (not recorded) was about 10%
    faster per thread, each would have needed more than 300 s even with enough memory, and the
    registered rule would have counted a timeout. So the recounts that change a reading are very
    unlikely. [A longer measurement, to 450 s, is running.]
- **The run.** 4,080 of 4,080 cells have valid records: 0 harness errors, 0 refusals, every call
  served by `claude-haiku-5-5`. One deviation (D1) resumed the run after the kernel's
  out-of-memory killer stopped it; it changed only the run procedure, and was committed in the
  middle of the held-out phase (after 2,202 of 4,080 records), not before it as section 9 asks.
  While the held-out process stalled under memory pressure, the registered 300 s verifier timeout
  fired late in 3 final checks (487, 572 and 721 s); none passed (section 7).
- **Checks.** An independent recomputation from the raw records reproduces every registered number
  exactly. Lean re-checked 619 recorded proofs after the run and all 619 gave the same verdict; of
  the 19 checks the memory problem touched, 2 now pass (the artifacts above), 16 were killed for
  memory again and 1 timed out.
- **Spend:** $17.33 in records across all phases; $20.54 with the 4 cells interrupted by the crash;
  cap $35.

## 2. What ran

| | |
|---|---|
| Model | `claude-haiku-5-5`, effort medium, adaptive thinking, max_tokens 32,000, no fallback model |
| Problems | 204 miniF2F test problems never run by us before (`split.json`) |
| Arms | B: 1 call. F: up to 3 feedback rounds (4 calls). R4: up to 4 fresh tries. P: as F, with a fixed sentence instead of Lean's report |
| Repetitions | 5 per problem per arm, 4,080 cells, one seeded interleaved order (the records follow it exactly) |
| Verifier | a fresh Lean 4.24.0 process with Mathlib `f897ebc`: statement unchanged, no `sorry`, axioms within `propext`, `Classical.choice`, `Quot.sound`; 300 s timeout |
| Held-out run | 2026-11-25T17:17:06Z to 2026-11-26T01:23:33Z, stopped by the kernel's out-of-memory killer; resumed under D1 at 02:15:32Z, complete at 09:08:30Z |
| Data | 204 usable and 204 complete problems; no missing cell in any arm (6.1) |

## 3. Registered results (sections 6.2-6.3)

Per-problem verified rate over 5 repetitions; mean over 204 problems; points of verified rate.

| Contrast | Rates | Estimate | 95% interval | 90% interval | Higher / lower / equal | Sign-flip p | Holm p | Reading |
|---|---|---|---|---|---|---|---|---|
| **H1 F − R4** (primary) | 92.06% → 93.43% | **+1.37** | +0.10 to +2.94 | +0.20 to +2.65 | 8 / 4 / 192 | 0.093 | – | no difference of 3 points or more |
| H2 F − P | 92.06% → 93.43% | +1.37 | +0.10 to +2.75 | +0.29 to +2.55 | 11 / 4 / 189 | 0.062 | 0.125 | no difference of 3 points or more |
| H3 P − R4 | 92.06% → 92.06% | 0.00 | −1.18 to +1.37 | −0.98 to +1.08 | 7 / 9 / 188 | 1.000 | 1.000 | no difference of 3 points or more |

- **How the readings follow the registration.** H1: p ≥ 0.05 and the 90% interval lies within
  (−3, +3) points. H2 and H3: Holm-adjusted p ≥ 0.05 and the 95% interval within (−3, +3).
  Attribution (6.3): neither H1 nor H2 reads "difference", so nothing is attributed.
- **Beside the readings (6.2).**
  - R4's unverified share is 7.9% (P's is the same, 7.9%), so a 3-point margin is about 38% of
    the room left.
  - The exact sign test on the 12 problems that differ in H1 gives p = 0.39 (8 against 4).
- **The 95% interval and the test.** At the registered seed, H1's 95% bootstrap interval is
  +0.10 to +2.94, which excludes zero, but both ends depend on the seed. Exploratory: across seeds
  1-150 the lower end is 0.00 for 14 seeds and the upper end +3.04 for 89. The registered test
  decides whether there is a difference; the interval is not read as a test. The registration
  fixed before the data that H1's "no difference" part is read on the 90% interval (6.2); its upper
  end is +2.65 to +2.75 across those seeds, so the reading does not depend on the seed.
- **Flags next to H1-H3:** none. First-call parity holds (6.5.1), and none of the integrity rates
  6.5.4 names differs by more than 1 point between F and R4, or F and P.

### Problems whose rates differ in H1

| Problem | R4 | F | | Problem | R4 | F |
|---|---|---|---|---|---|---|
| mathd_numbertheory_175 | 0.0 | 1.0 | | aime_1990_p15 | 1.0 | 0.8 |
| amc12a_2021_p14 | 0.0 | 0.8 | | amc12a_2020_p9 | 0.2 | 0.0 |
| aime_1987_p5 | 0.6 | 1.0 | | imo_1977_p6 | 1.0 | 0.8 |
| mathd_algebra_184 | 0.6 | 1.0 | | imo_2001_p6 | 0.2 | 0.0 |
| mathd_numbertheory_427 | 0.6 | 1.0 | | | | |
| aime_1999_p11 | 0.8 | 1.0 | | | | |
| imo_1974_p3 | 0.0 | 0.2 | | | | |
| mathd_numbertheory_711 | 0.8 | 1.0 | | | | |

- Two problems carry most of the estimate: `mathd_numbertheory_175` (0/5 to 5/5) and
  `amc12a_2021_p14` (0/5 to 4/5) add 0.9 of the 1.4 points.
- One of F's 4 losses, `aime_1990_p15`, comes from a cell the memory problem ended; its proof
  passes when re-checked (section 7).
- One of F's 8 wins, `aime_1999_p11`, includes a cell (repetition 4) that the crash interrupted and
  that ran again under section 8; the new attempt was verified (`d1_report.md`, section 3).
- The lists for H2 (15 problems), H3 (16) and the comparisons with the baseline (52, 52 and 54) are
  in `analysis.json` (`secondary`, `descriptive.vs_baseline`).

## 4. Descriptive results and diagnostics (6.4, 6.5)

Descriptive: none is a test of a hypothesis, and their p-values are not adjusted.

- **Against the baseline (one call):**

  | Contrast | Estimate | 95% interval | Higher / lower / equal | Sign-flip p |
  |---|---|---|---|---|
  | R4 − B | +8.24 | +6.08 to +10.59 | 52 / 0 / 152 | 4 × 10⁻¹⁶ |
  | F − B | +9.61 | +6.96 to +12.45 | 52 / 0 / 152 | 4 × 10⁻¹⁶ |
  | P − B | +8.24 | +6.08 to +10.59 | 53 / 1 / 150 | 4 × 10⁻¹⁵ |

  Six B cells were ended by the memory problem: 5 of `mathd_numbertheory_451` and 1 of
  `imo_2019_p1`, whose proof passes when re-checked. Counting that one as verified, each of these
  falls by 0.1 point; counting all six, by at most 0.6 (section 7).
- **F − R4 by the baseline's rate:**
  - 139 problems B always proved: no difference (both 100%).
  - 49 problems B sometimes proved: +2.0 points (−0.8 to +5.3).
  - 16 problems B never proved: +11.3 points (−2.5 to +28.8), F 18.8% against R4 7.5%. Too few
    problems to say whether feedback helps more on hard problems.
- **Headroom-scaled F − R4** (the mean difference over R4's mean unverified share): 17% (95%
  interval 1% to 38%; 10,000 of 10,000 resamples used).
- **Post-first-call gain.** On the rate gained after round 1, F − R4 is +2.9 points (+0.3 to +5.7),
  sign-flip p = 0.038, exact sign test p = 0.53 (35 against 29 problems). It is descriptive (6.4),
  reuses H1's attempts, and changes no reading. It is not free of first-call noise: F's first calls
  happened to fail more often (185 against 169), which leaves more room to gain. Exploratory: at
  R4's number of first-call failures the gain would be about +1.9 points.
- **First-call parity (6.5.1).** The identical first request passed in the loop at 81.9% (F),
  83.4% (R4) and 83.4% (P). No pair differs (Holm p ≥ 0.61): no flag.
- **Rescue after a failed first call (6.5.2).**

  | | F | R4 | P |
  |---|---|---|---|
  | Verified after a failed round 1 | 119/185 (64.3%) | 88/169 (52.1%) | 88/169 (52.1%) |
  | – no proof in round 1 | 6/51 | 6/50 | 4/46 |
  | – timeout | 2/7 | 4/9 | 3/7 |
  | – unknown name | 11/16 | 6/12 | 6/14 |
  | – other Lean error or rejection | 100/111 | 72/98 | 75/102 |

  F − R4 is +12.3 points (95% interval +3.3 to +21.7) and F − P +12.3 (+3.9 to +21.0); 10,000 of
  10,000 resamples used. This pools attempts, weighs problems with many first-call failures more,
  and reuses H1's attempts, so it is not a separate finding. F's extra rescues were mostly in the
  "other Lean error" class (100/111 against 72/98). The "unknown name" class, where repairs might
  come from memorized proofs written for an older Mathlib (registration section 10), is 16 of F's
  185 failed first calls.
- **Drift (6.5.3).** Verified share by quarter of start time:

  | Quarter | B | F | R4 | P |
  |---|---|---|---|---|
  | 1 | 86.8% | 91.8% | 89.9% | 95.3% |
  | 2 | 81.8% | 94.0% | 90.8% | 93.2% |
  | 3 | 83.3% | 93.8% | 93.7% | 90.2% |
  | 4 | 83.4% | 94.2% | 94.0% | 89.6% |

  The quarters hold different problems. Exploratory: taking, for each cell P drew, the other loop
  arms' rates on that problem predicts 95.7, 93.0, 91.4 and 90.9% for P, so the problem mix accounts
  for about 4.9 of P's 5.7-point fall; likewise for about 3.9 of R4's 4.1-point rise. No drift test
  was registered, and no time effect is claimed.
- **Integrity (6.5.4), over all 4,080 valid records:**

  | | B | F | R4 | P |
  |---|---|---|---|---|
  | Statuses | 1,020 completed | 1,020 completed | 1,020 completed | 1,020 completed |
  | Stops | 1,020 exhausted (1 call) | 949 in-loop pass, 71 exhausted | 930 / 90 | 928 / 92 |
  | Refusals, calls by another model, calls without response, harness errors, capped or errored attempts | 0 | 0 | 0 | 0 |
  | Calls stopped at the token limit | 44 | 155 | 161 | 137 |
  | Dropped-context rounds | 0 | 0 | 0 | 0 |
  | In-loop timeouts (rounds) | 0 | 17 | 22 | 18 |
  | Verifier timeouts | 6 | 1 | 2 | 1 |
  | Final checks killed by the kernel, exit 137 (D1; not in the row above) | 4 | 5 | 3 | 3 |
  | Verified attempts whose Lean check took over 120 s (Lean time, without the wait for a slot) | 1 | 4 | 4 | 2 |

  None of the rates 6.5.4 names (capped or errored attempts, verifier timeouts, dropped-context
  rounds, in-loop timeouts, harness errors) differs by more than 1 point between F and R4 or F and
  P. Calls stopped at the token limit, which 6.5.4 does not name, differ by 1.8 points per attempt
  between F (155) and P (137). Of the verifier timeouts, 4 also coincide with a memory event (B 2,
  F 1, R4 1; section 7). One in-loop check (R4) was killed by the kernel.

## 5. Sensitivity of H1 (6.6)

| Variant | Problems | Estimate | 95% interval | 90% interval | Higher / lower / equal | Sign-flip p |
|---|---|---|---|---|---|---|
| Primary | 204 | +1.37 | +0.10 to +2.94 | +0.20 to +2.65 | 8 / 4 / 192 | 0.093 |
| Without the 8 defect-list problems | 196 | +1.43 | +0.10 to +3.06 | +0.31 to +2.76 | 8 / 4 / 184 | 0.093 |
| Without the 5 problems with a verifier timeout | 199 | +1.31 | +0.00 to +2.91 | +0.20 to +2.61 | 7 / 3 / 189 | 0.109 |
| Complete cases | 204 | as primary | | | | |
| Exact sign test | 12 | | | | 8 / 4 | 0.39 |

The 5 problems with a verifier timeout: `aime_1990_p15`,
`algebra_apbpceq2_abpbcpcaeq1_aleq1on3anbleq1ancleq4on3`, `amc12a_2020_p7`,
`mathd_numbertheory_427`, `mathd_numbertheory_451`.

## 6. Cost (6.7; descriptive)

| | B | F | R4 | P |
|---|---|---|---|---|
| Total | $2.45 | $4.72 | $5.01 | $4.83 |
| Per attempt | $0.0024 | $0.0046 | $0.0049 | $0.0047 |
| Per verified proof | $0.0029 | $0.0050 | $0.0053 | $0.0051 |
| Calls per attempt | 1.00 | 1.33 | 1.36 | 1.36 |
| Output tokens | 4.69 M | 9.09 M | 9.80 M | 9.28 M |

- **F/R4 cost per verified proof:** 0.93 (95% interval 0.86 to 0.99; 10,000 of 10,000 resamples).
  F's total cost was 0.94 of R4's and it verified 14 more attempts. The arms were matched on the
  call cap, not on cost.
- **Total spend:** $17.33 from the records (smoke $0.05, setup check $0.27, held-out $17.01), and
  $20.54 with the 4 cells interrupted by the crash ($0.80 each, charged in full). The projection at
  registration was about $14 ($5-27); the cap was $35.

## 7. The deviation, and what the memory problem did

**D1** (`DEVIATIONS.md`) is the only deviation. It was committed **in the middle of the held-out
phase**, after 2,202 of 4,080 records; section 9 asks for deviations before the phase they affect.
It resumed the phase after the kernel's out-of-memory killer stopped it, and changed the run
procedure (how the supervisor resumes and cleans up). The design, arms, prompts, verifier rules,
timeouts and analysis did not change. The first 2,202 records ran under the registered procedure,
the other 1,878 under D1's. A note added after the phase (`DEVIATIONS.md`, "After the phase")
records where D1's expectations were wrong.

- **Cause.** Every final proof of `mathd_numbertheory_451`, in every arm, checks a bounded statement
  with `decide +kernel` over all m < 2020 (m < 2019 in two of them). Its Lean checks grew to
  13.2-13.9 GB, against the 14.3 GB memory limit the study's processes share.
- **What the kernel did** (`d1_report.md`): 18 out-of-memory events, killing 21 Lean processes and,
  once (01:23:33), the held-out process, which interrupted 4 cells; those had no record, were
  charged in full, and ran again on resuming.
- **Verdicts the memory problem set:**
  - 17 of the 20 final checks of `mathd_numbertheory_451` (F 5, R4 4, P 3, B 5): killed, or timed
    out just after a kill. The other 3 ended by an ordinary timeout or a Lean error.
    `mathd_numbertheory_451` is 0/5 in every arm. Three of the 17 (R4 repetition 2, B repetitions 0
    and 1) had run about 300 s or more when they were killed (487, 721 and 300 s to their record),
    so they would most likely be "not verified" by timeout in any case; the bounds below count them
    anyway.
  - **`aime_1990_p15`, F, repetition 3.** The same proof had passed Lean in the loop in 18.5 s. Its
    final check ran during the 04:50 memory event and was ended by a timeout that fired late, after
    571.8 s. It is the only cell in the study whose proof passed in the loop but not in the final
    check, and it is one of F's 4 losses in H1 and in H2. Re-checked alone after the run, the
    proof passes.
  - **`imo_2019_p1`, B, repetition 1.** Killed by the kernel after 126.7 s, under its timeout.
    Re-checked alone, the proof passes. It affects only the comparisons with the baseline.
  - One in-loop check (R4, `mathd_numbertheory_234`) was killed; the cell was verified anyway.
- **The registered 300 s timeout was not enforced in 3 final checks.** Under memory pressure the
  held-out process, which times the checks, stalled, and three timeouts fired late (487, 572 and
  721 s). None of the three produced a pass.
- **Bounds on the mean differences.** Each cell is 0.098 points of a mean difference. If every
  memory-set "not verified" verdict had been a pass, H1 and H2 could rise by at most 0.59 points
  (F's 6 cells) and fall by at most 0.39 and 0.29; H3 move by at most 0.39; the comparisons with
  the baseline fall by at most 0.59. D1 had said "only through `mathd_numbertheory_451`, at most
  0.5 points"; that was wrong.
- **Exploratory readings that count these cells as passes (not registered).** D1 bounded the mean
  differences, not the p-values:

  | If these cells had passed | H1 estimate, p | H1 reading | H2 Holm p | H2 reading |
  |---|---|---|---|---|
  | none (the registered result) | +1.37, 0.093 | no difference of 3 points or more | 0.125 | no difference of 3 points or more |
  | F's `aime_1990_p15` cell only (it passes when re-checked) | +1.47, 0.062 | no difference of 3 points or more | 0.080 | no difference of 3 points or more |
  | F's 5 killed cells of `mathd_numbertheory_451` | +1.86, 0.046 | difference (positive) | 0.062 | inconclusive |
  | those 5 and the `aime_1990_p15` cell | +1.96, 0.031 | difference (positive) | 0.040 | difference (positive) |
  | all 17 killed cells of `mathd_numbertheory_451` (every arm) | +1.47, 0.068 | no difference of 3 points or more | 0.073 | inconclusive |
  | those 17 and the `aime_1990_p15` cell | +1.57, 0.043 | difference (positive) | 0.045 | difference (positive) |

  H3 reads "no difference of 3 points or more" in every row. In the two rows where both H1 and H2
  read "difference (positive)", the attribution rule (6.3) would credit Lean's report; in every
  other row, as in the registered result, nothing is attributed.

  The smallest recounts that bring H1 below 0.05 are: the `aime_1990_p15` cell and any one of F's 5
  killed cells of `mathd_numbertheory_451` (p = 0.043); or, without the `aime_1990_p15` cell, any
  three of those 5 (p = 0.048). One of them alone gives p = 0.068 (`d1_report.json`).

  The registered rule counts a memory-ended check as not verified, so the registered readings
  stand. With the `aime_1990_p15` cell counted as a pass, these exploratory readings change only if
  one of F's killed proofs of `mathd_numbertheory_451` would pass. Very likely none of them could
  have passed within the registered 300 s (section 8, with its caveats), so the rows that change a
  reading very likely do not occur. Counting the two known artifacts as passes, every reading is
  the same as the registered one: H1 +1.47, p = 0.062; H2 Holm p = 0.080; H3 unchanged.
- **Exploratory checks D1 promised** (details in `d1_report.md`):

  | H1 variant | Problems | Estimate | 90% interval | Sign-flip p | Reading under the registered rule |
  |---|---|---|---|---|---|
  | Without `mathd_numbertheory_451` | 203 | +1.38 | +0.30 to +2.66 | 0.093 | no difference of 3 points or more |
  | Without the 476 (problem, repetition) pairs with a cell that started while a cell of `mathd_numbertheory_451` was running | 197 | +1.61 | +0.08 to +3.34 | 0.117 | inconclusive |
  | Without the 8 pairs with a memory-matched check (added after the phase) | 203 | +1.48 | +0.39 to +2.76 | 0.062 | no difference of 3 points or more |

  H2 and H3 without `mathd_numbertheory_451`: +1.38 (Holm p 0.125) and 0.00 (p 1). The second row
  drops 47% of the data, which is why its interval reaches past +3 points.
- **Exposure.** 602 of 4,080 cells (15%) started while a cell of `mathd_numbertheory_451` was
  running (from its start to its record). Every final check that waited more than 60 s for a Lean slot was among them (up to
  174 s, against at most 8 s otherwise). Tables by arm are in `d1_report.md`.
- **Evidence:** `oom_kernel_log.txt`, `held_out_snapshot/kernel_oom_boot_2026-11-25T17-15-47.txt`,
  `held_out_run_log_1.txt`, `held_out_run_log_2.txt`, `d1_report.py`, `d1_report.json`,
  `d1_report.md`, `recheck_killed.json`, `recheck_kernel_oom.txt`, `recheck_heavy_cpu.json`.

## 8. Verification after the run (exploratory checks, not registered)

- **Independent recomputation.** An independent reviewer rebuilt every statistic of 6.1-6.7 from
  the raw records with their own code: 346 comparisons with `analysis.json`, all equal. The
  sign-flip p-values match exactly (H1 = 95/1024), and every bootstrap interval reproduces with
  seed 20261125. Across 150 other seeds, H1's 90% upper bound stays at +2.65 to +2.75 points, so
  the reading does not depend on the seed.
- **Record integrity.** 4,080 unique cells (4 arms × 204 × 5), configurations matching the freeze,
  5,159 calls all served by `claude-haiku-5-5`, 0 refusals, charges equal to the calls' costs,
  every pass with exit code 0, an unchanged statement, no `sorry` and allowed axioms, proof hashes
  consistent, the run order equal to the seeded order, and the 4 dangling reservations exactly the
  interrupted cells. The per-attempt cost limit differs by arm only because it follows each arm's
  call cap (B $0.20, others $0.80); it never bound.
- **Registration review.** The readings and the attribution follow 6.2-6.3 exactly. The review
  found the problems corrected in this draft: D1's wrong expectation, the description of the
  `aime_1990_p15` check, the drift and post-first-call wording, and missing items now in
  `d1_report.md`.
- **Lean re-checks of recorded proofs** (`recheck.py`; the registered verifier, no model call):
  - 379 cells (every arm and repetition) of the 19 problems whose rates differ in H1, H2 or H3,
    except F's `aime_1990_p15` repetition 3, which is re-checked with the memory-touched checks
    below: all 379 verdicts repeat.
  - 240 further cells drawn at random (60 per arm, seed 20261126): all 240 repeat.
  - The 19 memory-touched final checks, one at a time (`--workers 1 --order light-first`;
    09:59-11:10Z): `aime_1990_p15` (F, 9.1 s) and `imo_2019_p1` (B, 8.5 s) now pass. All 17 of
    `mathd_numbertheory_451` fail again: the kernel killed 16 of them, each the only Lean process
    running, at 14.0 GB after 215-290 s (`recheck_kernel_oom.txt`), and 1 (F repetition 1) ran its
    full 300 s and timed out. In the run, two other checks of this problem ran out the 300 s without
    being killed, and 42 of its 45 in-loop checks ran out their 120 s. During the first heavy
    checks, until about 10:20, light commands also ran in this session (`d1_report.py`, file edits,
    a commit, progress checks); after that nothing else ran.
  - **Would F's other 4 proofs of `mathd_numbertheory_451` (repetitions 0, 2, 3, 4) check within
    300 s with enough memory?** No machine with more than 16 GB was available, so a 24 GB swap file
    lifted the memory limit and `recheck_heavy_cpu.py` measured CPU time instead (12:21-13:43Z, one
    proof at a time; `recheck_heavy_cpu.json`). A thread's user time cannot exceed the wall-clock
    time that passes, and it leaves out the paging work, which counts as system time (213-226 s per
    run). Each of the 4 passed 330 s of user CPU in one thread without finishing (after 851-926 s of
    wall-clock time), with its memory still growing (21.7-22.4 GB when stopped; it had passed 14 GB
    after 141-146 s of user CPU). So on the CPU measured (Intel Xeon, 2.1 GHz, in a new container
    after the restart; the run's own CPU was not recorded) each would very likely have needed more
    than 300 s even with enough memory, and the registered rule would have counted a timeout. The
    margin is small: the runs stopped at 330 s, so the conclusion fails if memory pressure added
    about 30 s of user time (about 10%), or if the run's CPU was about 10% faster per thread. The
    samples do not exclude an inflation of that order. [A longer measurement, to 450 s of user CPU
    and including F's repetition 1, is running.] A first version of the measurement, which counted
    user plus system time, was stopped and discarded (`recheck_heavy_cpu_v1_discarded.json`).
    Corroborating it, weakly: the fifth proof ran out the 300 s in a wall-clock re-check; and no
    check of any proof of this problem has ever passed: 0 of 82 checks that reached a verdict (45
    in-loop with a 120 s limit, 20 final, 17 re-checks; at most 54 distinct proofs), plus the 4
    measurements stopped unfinished.

## 9. What this study cannot show (registration section 10)

- **This setup only:** one model (Claude Haiku 5.5), one effort level, one prompt, a cap of 4 calls,
  and public miniF2F problems.
- **No long agent loops.**
- **Matched on the call cap,** not on calls made, tokens or cost (section 6).
- **Contamination may interact with the arms:** Lean's report can help repair a memorized proof
  written for an older Lean or Mathlib, which fresh tries cannot do. So the effect on these public
  problems may differ from that on unseen problems. The rescue split by failure class (section 4)
  shows how much of the effect such failures carry.
- **P controls for Lean's report only with its one fixed sentence.**
- **"No difference of 3 points or more"** is about the overall verified rate at this cap. It does
  not show that feedback never helps, nor anything about harder problems (the 16 problems the
  baseline never solved are too few).

## 10. Disclosures (registration section 12)

- This study was designed, coded, run and analysed with the help of Claude, a model by Anthropic;
  the model under test is also Anthropic's.
- Funding and affiliations: [owner to fill in].
- If the results are released, all code, records, transcripts of prompts and outputs, and the
  analysis are released with them, whatever they show.

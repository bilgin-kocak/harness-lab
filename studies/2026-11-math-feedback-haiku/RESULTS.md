# Results: does showing a failed proof with Lean's report beat fresh tries? (Claude Haiku 5.5, miniF2F)

- **Status: draft for the owner's review, 2026-11-26. Not published.** No claim from this file may
  be made public until the owner approves it.
- **Registration:** `REGISTRATION.md` (commit 64abcd7, 2026-11-25), before any paid call.
- **Registered result:** `analysis.json` and `analysis_stdout.txt`, the output of `analyze.py`
  run unchanged; every file of section 11 matches its registered SHA-256.
- **Everything marked "exploratory (not registered)"** was added after the data were seen. It
  changes no registered reading.

## 1. Bottom line

- **Primary (H1).** With the same cap of 4 calls, showing the model its failed proof with Lean's
  report (F) did **not** beat fresh tries (R4) by the registered margin of 3 points.
  - F verified 93.4% of attempts, R4 92.1%: a difference of **+1.4 points** (95% interval +0.1 to
    +2.9; 90% interval +0.2 to +2.7).
  - The registered test does not find a difference: two-sided sign-flip p = 0.093 (8 problems
    higher with F, 4 higher with R4, 192 equal).
  - The registered reading is **"no difference of 3 points or more in the overall verified rate"**.
    It does not say the difference is zero.
- **Secondary (H2, H3).** F against P (Lean's report replaced by "Lean did not accept this proof.")
  and P against fresh tries (R4) both read "no difference of 3 points or more". F − P is +1.4 points
  (Holm p = 0.12); P − R4 is 0.0 (p = 1). So no part of any effect is attributed to Lean's report
  (6.3).
- **Against one call (descriptive).** Every arm with 4 calls verified about 8-10 points more than
  the single-call baseline (B 83.8%; R4 and P 92.1%; F 93.4%).
- **The pilot's gap did not hold up.** The pilot (40 problems, exploratory) had F 5.8 points above
  R4, and this study was powered for +4.1 (96%). On 204 fresh problems the estimate is +1.4, and
  the 90% interval stops at +2.7.
- **The run.** 4,080 of 4,080 cells have valid records, with 0 harness errors, 0 refusals and every
  call served by `claude-haiku-5-5`. The run was stopped once by an out-of-memory kill and resumed
  under deviation D1. These events cannot move the registered differences by more than about
  0.6 points (section 6).
- **Spend:** $17.33 in records across all phases, $20.54 with the 4 cells interrupted by the crash,
  against the $35 cap.

## 2. What ran

| | |
|---|---|
| Model | `claude-haiku-5-5`, effort medium, adaptive thinking, max_tokens 32,000, no fallback model |
| Problems | 204 miniF2F test problems never run by us before (`split.json`) |
| Arms | B: 1 call. F: up to 3 feedback rounds (4 calls). R4: up to 4 fresh tries. P: F with a fixed sentence instead of Lean's report |
| Repetitions | 5 per problem per arm, 4,080 cells, one seeded interleaved order |
| Verifier | a fresh Lean 4.24.0 process, Mathlib `f897ebc`, statement unchanged, no `sorry`, axioms within `propext`, `Classical.choice`, `Quot.sound`; 300 s timeout |
| Held-out run | 2026-11-25T17:17:06Z to 2026-11-26T01:23:33Z (stopped by the kernel's out-of-memory killer); resumed 02:15:32Z to 09:08:30Z |

## 3. Registered results (section 6.2-6.3)

Per-problem verified rate over 5 repetitions; mean over 204 problems.

| Contrast | Arms' rates | Estimate | 95% interval | 90% interval | Higher / lower / equal | Sign-flip p | Holm p | Reading |
|---|---|---|---|---|---|---|---|---|
| **H1 F − R4** (primary) | 92.06% → 93.43% | **+1.37** | +0.10 to +2.94 | +0.20 to +2.65 | 8 / 4 / 192 | 0.093 | – | no difference of 3 points or more |
| H2 F − P | 92.06% → 93.43% | +1.37 | +0.10 to +2.75 | +0.29 to +2.55 | 11 / 4 / 189 | 0.062 | 0.125 | no difference of 3 points or more |
| H3 P − R4 | 92.06% → 92.06% | 0.00 | −1.18 to +1.37 | −0.98 to +1.08 | 7 / 9 / 188 | 1.000 | 1.000 | no difference of 3 points or more |

- **How the readings follow the registration.**
  - H1: p ≥ 0.05, and the 90% interval lies within (−3, +3) points.
  - H2 and H3: their Holm-adjusted p ≥ 0.05, and their 95% intervals lie within (−3, +3).
  - Attribution (6.3): neither H1 nor H2 reads "difference", so nothing is attributed.
- **Beside the reading (6.2).**
  - R4's unverified share is 7.9%, so a 3-point margin is about 38% of the room left to improve.
  - The exact sign test on the 12 problems that differ gives p = 0.39 (8 against 4). The two tests
    agree.
- **The 95% interval and the test.** H1's 95% percentile bootstrap interval (+0.10 to +2.94)
  excludes zero, while the registered sign-flip test gives p = 0.093. The registered test decides
  whether there is a difference; the interval is not a test, and it is not read as one. The honest
  summary is a small positive estimate that the registered test does not confirm, and an effect
  below 3 points.
- **Flags next to H1-H3:** none. First-call parity holds (6.5.1), and no integrity rate differs
  between F and R4, or F and P, by more than 1 point (6.5.4).

### Problems whose rates differ (H1)

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

Two problems carry most of the estimate: `mathd_numbertheory_175` (0/5 to 5/5) and
`amc12a_2021_p14` (0/5 to 4/5) together add 0.9 of the 1.4 points. H2's and H3's lists are in
`analysis.json`.

## 4. Descriptive results and diagnostics (6.4, 6.5)

All descriptive; none is a test of a hypothesis, and their p-values are not adjusted.

- **Against the baseline (one call):**

  | Contrast | Estimate | 95% interval | Higher / lower / equal |
  |---|---|---|---|
  | R4 − B | +8.24 | +6.08 to +10.59 | 52 / 0 / 152 |
  | F − B | +9.61 | +6.96 to +12.45 | 52 / 0 / 152 |
  | P − B | +8.24 | +6.08 to +10.59 | 53 / 1 / 150 |

- **F − R4 by the baseline's rate:**
  - 139 problems B always proved: no difference (both arms 100%).
  - 49 problems B sometimes proved: +2.0 points (−0.8 to +5.3).
  - 16 problems B never proved: +11.3 points (−2.5 to +28.8), with F at 18.8% and R4 at 7.5%.
- **Headroom-scaled F − R4:** 17% of R4's remaining failures (95% interval 1% to 38%).
- **Post-first-call gain:** F − R4 on the rate gained after round 1 is +2.9 points (+0.3 to +5.7),
  sign-flip p = 0.038, unadjusted. It removes the first-call noise the arms share. It reuses H1's
  attempts and is not a separate test; it does not change H1's reading.
- **First-call parity (6.5.1).** The identical first request passed in the loop at 81.9% (F),
  83.4% (R4) and 83.4% (P). No pair differs (Holm p ≥ 0.61), so there is no flag. F's first calls
  happened to do slightly worse, which is why the post-first-call gain is larger than H1's estimate.
- **Rescue after a failed first call (6.5.2).**

  | | F | R4 | P |
  |---|---|---|---|
  | Verified after a failed round 1 | 119/185 (64.3%) | 88/169 (52.1%) | 88/169 (52.1%) |
  | – no proof in round 1 | 6/51 | 6/50 | 4/46 |
  | – timeout | 2/7 | 4/9 | 3/7 |
  | – unknown name | 11/16 | 6/12 | 6/14 |
  | – other Lean error or rejection | 100/111 | 72/98 | 75/102 |

  F − R4 is +12.3 points (95% interval +3.3 to +21.7) and F − P +12.3 (+3.9 to +21.0). These pool
  attempts, weigh problems with many first-call failures more, and reuse H1's attempts: they
  describe where F's small advantage comes from (Lean errors other than timeouts), not a separate
  finding. Section 10 of the registration notes that "unknown name" repairs may come from
  memorized proofs written for an older Mathlib; that class is 16 of F's 185 failed first calls.
- **Drift (6.5.3).** Verified share by quarter of start time:

  | Quarter | B | F | R4 | P |
  |---|---|---|---|---|
  | 1 | 86.8% | 91.8% | 89.9% | 95.3% |
  | 2 | 81.8% | 94.0% | 90.8% | 93.2% |
  | 3 | 83.3% | 93.8% | 93.7% | 90.2% |
  | 4 | 83.4% | 94.2% | 94.0% | 89.6% |

  Each quarter holds about 255 attempts per arm, on different problems, so a quarter's share moves
  with its problem mix (a standard error of about 1.7 points before that). P falls by 5.7 points
  from the first to the last quarter and R4 rises by 4.1. Nothing in the design explains this, and
  it was not tested.
- **Integrity (6.5.4), over all 4,080 valid records:**

  | | B | F | R4 | P |
  |---|---|---|---|---|
  | Statuses | 1,020 completed | 1,020 completed | 1,020 completed | 1,020 completed |
  | Stops | 1,020 exhausted (1 call) | 949 in-loop pass, 71 exhausted | 930 / 90 | 928 / 92 |
  | Refusals, calls by another model, calls without response, harness errors | 0 | 0 | 0 | 0 |
  | Calls stopped at the token limit | 44 | 155 | 161 | 137 |
  | Dropped-context rounds | 0 | 0 | 0 | 0 |
  | In-loop timeouts | 0 | 17 | 22 | 18 |
  | Verifier timeouts | 6 | 1 | 2 | 1 |

  No rate differs by more than 1 point between F and R4 or F and P.

## 5. Sensitivity of H1 (6.6)

| Variant | Problems | Estimate | 95% interval | 90% interval | Higher / lower / equal | Sign-flip p |
|---|---|---|---|---|---|---|
| Primary | 204 | +1.37 | +0.10 to +2.94 | +0.20 to +2.65 | 8 / 4 / 192 | 0.093 |
| Without the 8 defect-list problems | 196 | +1.43 | +0.10 to +3.06 | +0.31 to +2.76 | 8 / 4 / 184 | 0.093 |
| Without the 5 problems with a verifier timeout | 199 | +1.31 | +0.00 to +2.91 | +0.20 to +2.61 | 7 / 3 / 189 | 0.109 |
| Complete cases | 204 | as primary | | | | |

The 5 problems with a verifier timeout: `aime_1990_p15`,
`algebra_apbpceq2_abpbcpcaeq1_aleq1on3anbleq1ancleq4on3`, `amc12a_2020_p7`,
`mathd_numbertheory_427`, `mathd_numbertheory_451`.

## 6. Cost (6.7)

| | B | F | R4 | P |
|---|---|---|---|---|
| Total | $2.45 | $4.72 | $5.01 | $4.83 |
| Per attempt | $0.0024 | $0.0046 | $0.0049 | $0.0047 |
| Per verified proof | $0.0029 | $0.0050 | $0.0053 | $0.0051 |
| Calls per attempt | 1.00 | 1.33 | 1.36 | 1.36 |
| Output tokens | 4.69 M | 9.09 M | 9.80 M | 9.28 M |

- **F/R4 cost per verified proof:** 0.93 (95% interval 0.86 to 0.99). F was about 7% cheaper per
  verified proof than fresh tries, because it stopped a little earlier and produced fewer output
  tokens.
- **Total spend:** $17.33 from the records (smoke $0.05, setup check $0.27, held-out $17.01), and
  $20.54 with the 4 cells interrupted by the crash ($0.80 each, charged in full). The projection at
  registration was about $14 ($5-27); the cap was $35.

## 7. Deviations and problems in the run

The only deviation is **D1** (`DEVIATIONS.md`): resuming the held-out phase after the kernel's
out-of-memory killer stopped it, with a changed run procedure. The design, arms, prompts, verifier
rules, timeouts and analysis did not change.

- **Cause.** Final checks of proofs of one problem, `mathd_numbertheory_451`, grew to 13.2-13.9 GB,
  close to the 14.3 GB memory limit the study's processes share.
- **What it did.**
  - 18 out-of-memory events in all; 20 kernel kills of Lean processes, and one of the held-out
    process (01:23:33), which interrupted 4 cells. Those had no record and ran again on resuming.
  - 17 of the 20 final checks of `mathd_numbertheory_451` were ended by a kernel kill (5 in F, 4 in
    R4, 3 in P, 5 in B); the 3 others ended by an ordinary timeout or a Lean error. Under the
    registered verifier rule they are "not verified" in every arm.
  - **Not foreseen by D1:** at 04:04:17 the kernel's second kill ended the final check of a
    baseline cell of `imo_2019_p1` (exit 137 after 127 s). D1 said the out-of-memory verdicts could
    affect results only through `mathd_numbertheory_451`. That holds for H1-H3, which do not use
    the baseline, but not for the comparisons with the baseline (6.4).
  - One in-loop check (R4, `mathd_numbertheory_234`) was killed; that cell was verified anyway.
  - Under memory pressure the held-out process stalled, and three verifier timeouts fired late: an
    R4 and a B check of `mathd_numbertheory_451`, and an F check of `aime_1990_p15` (ended after
    572 s against 300 s). That F check was still running at 300 s, so it would have been a
    timeout without the stall too; whether memory pressure made it slower is not known.
- **How much this can matter.** Each cell is 1/5 of its problem's rate, so 0.098 points of a mean
  difference. If every kill-ended "not verified" verdict had been a pass:
  - H1 could rise by at most 0.49 points (5 F cells) or fall by at most 0.39 (4 R4 cells); with
    the late-timeout F cell, rise by at most 0.59;
  - H2: +0.49 (or +0.59) / −0.29; H3: +0.29 / −0.39;
  - the comparisons with the baseline: up to 0.59 points (6 B cells).
- **The readings depend on these cells (exploratory, not registered; added after the phase).** D1
  bounded the mean differences, not the p-values. H1's p = 0.093 sits close enough to 0.05 that the
  memory-touched cells can flip it:

  | If these cells had passed | H1 estimate, p | H1 reading | H2 Holm p | H2 reading |
  |---|---|---|---|---|
  | none (the registered result) | +1.37, 0.093 | no difference of 3 points or more | 0.125 | no difference of 3 points or more |
  | F's 5 kill-ended cells | +1.86, 0.046 | difference (positive) | 0.062 | inconclusive |
  | F's 5 and its late-timeout cell | +1.96, 0.031 | difference (positive) | 0.040 | difference (positive) |
  | R4's 4 kill-ended cells | +0.98, 0.318 | no difference of 3 points or more | 0.125 | no difference of 3 points or more |
  | every kill-ended cell (18) | +1.47, 0.068 | no difference of 3 points or more | 0.073 | inconclusive |
  | every kill-ended and late-timeout cell (19) | +1.57, 0.043 | difference (positive) | 0.045 | difference (positive) |

  H3 reads "no difference of 3 points or more" in every case. The registered rule counts a killed
  check as not verified, so the registered readings stand; but they rest on those 19 cells, and
  most of all on F's 6. Whether those proofs pass is a fact Lean can settle, so they were checked
  again after the run (section 8).
- **What the proofs of `mathd_numbertheory_451` are.** All 20 final proofs, in every arm, prove a
  bounded statement with `decide +kernel` over all m < 2020 (the sum of divisors). In the loop
  arms, 42 of their 45 in-loop checks timed out at 120 s and the other 3 failed with a Lean error;
  and two final checks (R4 and P) ran the full 300 s and timed out without being killed. That suggests these proofs do not check within the registered
  300 s even with enough memory, but it does not settle each proof.
- **Exploratory (not registered) checks that D1 promised:**

  | H1 variant | Problems | Estimate | 95% interval | 90% interval | Higher / lower / equal | Sign-flip p |
  |---|---|---|---|---|---|---|
  | Without `mathd_numbertheory_451` | 203 | +1.38 | +0.10 to +2.96 | +0.30 to +2.66 | 8 / 4 / 191 | 0.093 |
  | Without every (problem, repetition) with a cell that started during a `mathd_numbertheory_451` check (476 of 1,020 pairs) | 197 | +1.61 | −0.17 to +3.72 | +0.08 to +3.34 | 7 / 3 / 187 | 0.117 |
  | Without (problem, repetition) pairs with a kill-matched check (added after the phase) | 203 | +1.48 | +0.20 to +3.05 | +0.39 to +2.76 | 8 / 3 / 192 | 0.062 |

  H2 and H3 without `mathd_numbertheory_451`: +1.38 (p = 0.062) and 0.00 (p = 1). The second row
  drops 47% of the data, so its wider interval reaches past +3 points. None of these variants finds
  a difference at p < 0.05.
- **Exposure.** About 15% of cells (602 of 4,080) started while a `mathd_numbertheory_451` check
  ran. Those cells had all 5 of the final checks that waited more than 60 s for a Lean slot (up to
  174 s, against at most 8 s otherwise). In-loop and verifier timeouts were not markedly more
  frequent among them. Details by arm are in `d1_report.json`.
- **Evidence:** `oom_kernel_log.txt`, `held_out_snapshot/kernel_oom_boot_2026-11-25T17-15-47.txt`,
  `held_out_run_log_1.txt`, `held_out_run_log_2.txt`, `d1_report.py` and `d1_report.json`.

## 8. Verification after the run

[To be completed: independent recomputation, record integrity, registration review, and Lean
re-checks of recorded proofs.]

## 9. What this study cannot show (registration section 10)

- **This setup only:** one model (Claude Haiku 5.5), one effort level, one prompt, a cap of 4 calls,
  and public miniF2F problems.
- **No long agent loops.**
- **Matched on the call cap,** not on calls made, tokens or cost; those are in section 6.
- **Contamination may interact with the arms:** Lean's report can help repair a memorized proof
  written for an older Lean or Mathlib, which fresh tries cannot do.
- **P controls for Lean's report only with its one fixed sentence.**
- **"No difference of 3 points or more"** is about the overall verified rate at this cap. It does
  not show that feedback never helps, nor that it helps on harder problems (the 16 problems the
  baseline never solved are too few to say).

## 10. Disclosures (registration section 12)

- This study was designed, coded, run and analysed with the help of Claude, a model by Anthropic;
  the model under test is also Anthropic's.
- Funding and affiliations: [owner to fill in].
- If the results are released, all code, records, transcripts of prompts and outputs, and the
  analysis are released with them.

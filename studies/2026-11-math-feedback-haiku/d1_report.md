# D1 report: the out-of-memory kills, matched to cells (exploratory, not registered)

- **Written after the held-out phase ended, 2026-11-26**, with the records and outcomes by arm
  available. These are the reports `DEVIATIONS.md` D1 promised, plus checks added after the
  phase, each marked as added. Nothing here changes a record or a registered reading.
- **Sources:** `d1_report.py`, which writes `d1_report.json`; the kernel's reports in
  `held_out_snapshot/kernel_oom_boot_2026-11-25T17-15-47.txt` (excerpts and notes in
  `oom_kernel_log.txt`); and the Lean re-checks in `recheck_killed.json`.
- **Independent audit.** An independent recomputation (own code, from the raw files) reproduced
  every number in `d1_report.json`, the kills and their sizes, the matches, the exposure table, the
  exploratory contrasts and the bounds.

## 1. The kernel's kills

- **18 out-of-memory events** (10 before the stop at 01:23:33Z, 8 after the resume at 02:15:32Z).
- **22 processes killed:**
  - 18 Lean processes of 13.2-13.9 GB (the kernel's "kB" are KiB), the first process killed in
    each event;
  - 3 small Lean processes of 24 MB, 167 MB and 184 MB, killed right after a large one in the same
    event ("bystanders", under 1 GB);
  - the held-out process itself (132 MB, 01:23:33), which stopped the phase.

## 2. Killed checks, by arm and problem

**D1's rule, as written:** a final check counts when a kill falls in its cell's window and within
2 s of its end, whatever its exit code; an in-loop check counts when a kill falls in the window and
a round's in-loop reason is "lean exited with code 137" or "-9". By that rule, **19 final checks
and 1 in-loop check** match.

**One-to-one (added after the phase).** Two kills can fall within 2 s of the same check, and one
kill within 2 s of two checks, so each kill was assigned to the single process it killed. The
kernel's task tables support every assignment: the lake processes' IDs follow the checks' start
order, and the in-loop check's end time matches its kill within a fraction of a second.

| Check | Arm | Problem | Cells | How it ended |
|---|---|---|---|---|
| final | F | `mathd_numbertheory_451` | 5 | exit 137 |
| final | R4 | `mathd_numbertheory_451` | 4 | 3 exit 137; 1 late timeout (487 s, 17:39:59) |
| final | P | `mathd_numbertheory_451` | 3 | exit 137 |
| final | B | `mathd_numbertheory_451` | 5 | 3 exit 137; 2 timeouts just after a kill (720.7 s at 04:50:27; 300.0 s at 08:00:22) |
| final | B | `imo_2019_p1` | 1 | exit 137 after 126.7 s (bystander kill, 04:04:17) |
| in-loop | R4 | `mathd_numbertheory_234` | 1 | try 1 killed (bystander, 02:26:25); the cell was verified on try 4 |

**Not killed, but ended by the memory event:** the F final check of `aime_1990_p15` (rep 3),
which D1's time rule also matches. The kernel killed `mathd_numbertheory_451`'s check at 04:50:26,
not this one. But the same proof had passed Lean in the loop in 18.5 s, the other 19 final checks
of that problem took 5.6-8.0 s, and this check ran 571.8 s before the stalled verifier's timeout
ended it. It is the only cell in the study whose proof passed in the loop and failed the final
check.

**Unmatched kills:** the three at 01:23:33 (the 13.7 GB Lean check of the interrupted
`mathd_numbertheory_451` cell, the held-out process, and a 24 MB Lean process). The 4 interrupted
cells (section 3) were in progress.

**Next to the 6.5 timeouts.** Section 6.5's verifier-timeout counts (B 6, F 1, R4 2, P 1) already
include 4 of the checks above: R4's late timeout and B's two timeouts on `mathd_numbertheory_451`,
and F's `aime_1990_p15`. The checks the kernel killed with exit 137 (F 5, R4 3, P 3, B 4) are in
no 6.5 count.

## 3. Interrupted cells

The kernel killed the held-out process at 01:23:33Z. The 4 cells in progress had no record and
were charged their full reservation, $0.80096 each ($3.20 in all). They ran again first on
resuming.

| Arm | Problem | Repetition | Reserved | Re-run outcome |
|---|---|---|---|---|
| P | `mathd_numbertheory_451` | 4 | 01:11:38 | not verified (its final check was killed again, 02:26:25) |
| F | `aime_1999_p11` | 4 | 01:19:41 | verified |
| R4 | `imo_1969_p2` | 4 | 01:20:09 | verified |
| P | `amc12_2001_p5` | 2 | 01:21:07 | not verified |

D1 named only the first as a new attempt; all four are.

## 4. Exposure: cells that started while a check of `mathd_numbertheory_451` ran

Times are to the second. The inclusive rule counts a start in the same second as that cell's start
or end (602 cells); the strict rule does not (576). D1's earlier count of 373 records overlapping such
a cell used strict boundaries on the first 2,202 records.

| Arm | Cells exposed / not | In-loop timeouts | Verifier timeouts | Final checks waiting > 60 s for a Lean slot | Longest wait (s) |
|---|---|---|---|---|---|
| B | 156 / 864 | 0 / 0 | 3 / 3 | 2 / 0 | 173.6 / 4.2 |
| F | 141 / 879 | 2 / 15 | 1 / 0 | 2 / 0 | 118.7 / 7.3 |
| R4 | 156 / 864 | 4 / 18 | 0 / 2 | 0 / 0 | 27.0 / 7.9 |
| P | 149 / 871 | 4 / 14 | 0 / 1 | 1 / 0 | 136.1 / 6.8 |

(Inclusive rule; "exposed / not exposed". The strict rule's table is in `d1_report.json`.) In-loop
timeouts are counted per round, as in 6.5. Every long wait for a Lean slot was in an exposed cell.

## 5. Exploratory analyses D1 named

| Analysis | Problems | Estimate | 95% interval | 90% interval | Higher / lower / equal | Sign-flip p | Reading under the registered rule |
|---|---|---|---|---|---|---|---|
| H1 without `mathd_numbertheory_451` | 203 | +1.38 | +0.10 to +2.96 | +0.30 to +2.66 | 8 / 4 / 191 | 0.093 | no difference of 3 points or more |
| H2 without it (Holm p 0.125) | 203 | +1.38 | +0.20 to +2.76 | +0.30 to +2.56 | 11 / 4 / 188 | 0.062 | no difference of 3 points or more |
| H3 without it (Holm p 1) | 203 | 0.00 | −1.18 to +1.28 | −0.99 to +1.08 | 7 / 9 / 187 | 1.000 | no difference of 3 points or more |
| H1 without the 476 (problem, repetition) pairs with an exposed cell | 197 | +1.61 | −0.17 to +3.72 | +0.08 to +3.34 | 7 / 3 / 187 | 0.117 | inconclusive |
| the same, strict rule (463 pairs) | 197 | +1.61 | −0.08 to +3.64 | +0.17 to +3.30 | 7 / 3 / 187 | 0.107 | inconclusive |
| H1 without the 8 pairs with a matched check (added after the phase) | 203 | +1.48 | +0.20 to +3.05 | +0.39 to +2.76 | 8 / 3 / 192 | 0.062 | no difference of 3 points or more |

Dropping the exposed pairs removes 46-47% of the data, which is why its interval widens past
+3 points.

## 6. How much the memory events can matter

**Bounds on the mean differences.** Each cell is 1/5 of its problem's rate, so 0.098 points of a
mean difference over 204 problems. Counting every killed or memory-ended final check that is "not
verified" (F 6 with `aime_1990_p15`, R4 4, P 3, B 6):

| Contrast | Could rise by | Could fall by |
|---|---|---|
| H1 F − R4 | 0.59 | 0.39 |
| H2 F − P | 0.59 | 0.29 |
| H3 P − R4 | 0.29 | 0.39 |
| R4 − B, F − B, P − B | 0.29-0.59 | 0.59 |

D1 said these verdicts could move a registered mean difference "only through
`mathd_numbertheory_451`: by at most 1/204, about 0.5 points". That was wrong: `imo_2019_p1`
(B) and `aime_1990_p15` (F) were also affected. The `imo_2019_p1` cell affects only the
comparisons with the baseline (by 0.098 points each); it changes nothing in H1-H3.

**Readings (added after the phase).** H1's p = 0.093 is close enough to 0.05 that a few of these
cells can change the reading:

| If these cells had passed | H1 estimate, p | H1 reading | H2 Holm p | H2 reading |
|---|---|---|---|---|
| none (the registered result) | +1.37, 0.093 | no difference of 3 points or more | 0.125 | no difference of 3 points or more |
| F's `aime_1990_p15` cell only | +1.47, 0.062 | no difference of 3 points or more | 0.080 | no difference of 3 points or more |
| F's 5 killed cells of `mathd_numbertheory_451` | +1.86, 0.046 | difference (positive) | 0.062 | inconclusive |
| those 5 and the `aime_1990_p15` cell | +1.96, 0.031 | difference (positive) | 0.040 | difference (positive) |
| every killed check of `mathd_numbertheory_451` (17), in all arms | +1.47, 0.068 | no difference of 3 points or more | 0.073 | inconclusive |
| those 17 and the `aime_1990_p15` cell | +1.57, 0.043 | difference (positive) | 0.045 | difference (positive) |

H3 reads "no difference of 3 points or more" in every row. The smallest sets that bring H1 below
0.05 are the `aime_1990_p15` cell together with any one of F's 5 killed cells of
`mathd_numbertheory_451` (p = 0.043).

## 7. Lean re-checks of these proofs (added after the phase)

[Filled in from `recheck_killed.json` when the re-check ends.]

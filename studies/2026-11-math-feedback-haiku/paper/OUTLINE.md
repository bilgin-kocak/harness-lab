# Paper draft: outline and abstract

- **Status: draft for the owner, 2026-11-26. Not published, not submitted.** Every claim below
  needs the owner's approval before it leaves this repository.
- **Superseded in part by the full draft, `PAPER.md`.** Where they differ (the abstract's wording,
  the related work after each citation was checked, `REFERENCES_CHECK.md`), `PAPER.md` is current.
- **Sources:** `../RESULTS.md` (the results draft), `../analysis.json` (the registered output),
  `../d1_report.md`, `../REGISTRATION.md`, `../DEVIATIONS.md`, `../RELATED_WORK.md`, the pilot
  (`../../2026-11-math-pilot/REPORT.md`) and the earlier math study
  (`../../../docs/results/2026-11-23-math.md`). Each number here is taken from one of them.
- **Reviewed:** two independent reviews (a fact-check and a referee's reading), each finding
  checked by a second reviewer; all confirmed findings are applied in this version.
- **Form:** an arXiv preprint of about 8 pages plus appendices, then a workshop that takes careful
  evaluations or negative results.
- **Labels, as the registration requires (6.8):** everything not produced by `analyze.py` run
  unchanged is marked *exploratory (not registered)* in the paper, and changes no reading.

## Title options

1. *Feedback or just another try? A pre-registered test of Lean's error report against fresh
   attempts, with Claude Haiku 5.5 on miniF2F*
2. *Does the proof checker's error report help? A pre-registered comparison at a matched call cap*
3. *From pilot to pre-registration: a feedback estimate in LLM theorem proving that shrank*

Option 1 is the most accurate; option 3 leads with the method story.

## Abstract (draft, about 255 words; trim to the venue's limit)

> Showing a language model its failed proof with the proof checker's error messages is common in
> LLM theorem proving, but its gain is not always separated from that of simply trying again. We
> ask whether Lean's report helps beyond fresh attempts at the same cap of calls. In a
> pre-registered study, Claude Haiku 5.5 attempted 204 miniF2F problems in Lean 4, five times each,
> in four interleaved arms: one call (B); up to four calls showing the failed proof and Lean's
> report (F); up to four fresh tries (R4); and a placebo showing the failed proof with a fixed
> rejection sentence (P). F verified 93.4% of attempts and R4 92.1%: +1.4 points (90% CI +0.2 to
> +2.6; sign-flip p = 0.093), which the registered rule reads as no difference of 3 points or more
> in the overall verified rate; descriptively, F removed about 17% of R4's failures (95% CI 1% to
> 38%). F versus P (+1.4, Holm p = 0.12) and P versus R4 (0.0) read the same way. Arms capped at
> four calls (1.3 to 1.4 used on average) verified 8 to 10 points more than one call. An exploratory
> pilot had estimated +5.8 points for F over R4; our 90% interval excludes it and the +4.1 points
> the study was powered for. Out-of-memory events ended 19 of the 4,080 final checks, 17 of them on
> one problem no arm proved; under the registered rule every reading stands, and exploratory
> re-checks indicate that the recounts that would change a reading are very unlikely. Code,
> records, transcripts and analysis are released.

**Words that need care:** "removed about 17% of the failures" is the registered headroom-scaled
descriptive, not a test; "1.3 to 1.4 calls" (F 1.33, R4 1.36, P 1.36); "the recounts that would
change a reading are very unlikely" rests on the exploratory re-checks, the CPU-time
measurement and the speed comparison (section 6), with their 1.5-times margin, the swap caveat
and the assumption that the speed ratio from ordinary checks carries over to this proof.
Trim to the venue's limit.

## Outline

### 1. Introduction (1 page)
- Repair loops with checker feedback are common in LLM proving. A gain measured against one call,
  as in our own earlier study, mixes feedback with extra attempts; call- or sample-matched
  comparisons in prior work disagree (Klingner et al.; Li & Li; Baldur; Kimina-Prover).
- Question: at a matched cap of 4 calls, does Lean's report help, and does it help because of the
  report itself or because the model sees its failed attempt?
- Contributions:
  1. a pre-registered comparison at a matched cap of 4 calls, with a placebo arm (4,080 attempts,
     204 problems, 5 repetitions, interleaved, exact tests);
  2. the answer for this setup: no difference of 3 points or more in the overall verified rate,
     with a small positive estimate, and no attribution to the report;
  3. a worked case of a pilot estimate (+5.8 points, 40 reused problems, arms run in separate time
     blocks) shrinking to +1.4 in a larger pre-registered, interleaved study on 204 new problems,
     and of why our own earlier comparison with one call could not separate feedback from extra
     attempts;
  4. how out-of-memory kills on the checking machine were handled under rules fixed before the
     outcomes were seen (a killed check is not a pass; the registration re-runs only API
     outages), bounded and reported.

### 2. Related work (1 page)
- Call-matched refinement against resampling: Klingner et al. (arXiv 2606.05632): Δ_32 averages
  +7.7 points on a 50-problem miniF2F subset (14 models, one refine run per problem) and −1.4 on
  miniCTX; set against our +1.4 at a cap of 4.
- Li & Li (arXiv 2610.22698), open models on difficult miniF2F problems: no tested one-step
  feedback condition, nor sequential repair at 64 attempts, established an advantage over
  instruction-only resampling. Their "failed predecessor" and "compiler error" conditions are
  analogous to our P and F.
- Self-repair in code: Olausson et al. (arXiv 2306.09896): gains often modest, varying between
  subsets and sometimes absent, once repair's cost is counted (matched on samples).
- Repair gains at matched samples: Baldur (Isabelle/HOL), Kimina-Prover (blog, 59 hard problems,
  no variance reported); Goedel-Prover-V2's ablation of error messages (no resampling arm; the
  closest prior analogue of P).
- miniF2F is near saturation for the strongest systems (Seed-Prover).
- To add, each citation checked before use: self-correction (e.g. Huang et al., "Large Language
  Models Cannot Self-Correct Reasoning Yet"; Kamoi et al.'s survey); repeated sampling as the
  natural baseline (e.g. Brown et al., "Large Language Monkeys"); provers using compiler feedback
  (e.g. DeepSeek-Prover-V1.5); equivalence testing (Lakens 2017), since the three-way reading is
  close to one; pre-registration in ML.
- What is new here: pre-registration, a multi-round cap of 4 calls, 5 repetitions on each of 204
  problems, and a current API model. We found no study with all of these, but our search was not
  systematic.

### 3. Setup (1 page)
- Model and settings: `claude-haiku-5-5`, effort medium, adaptive thinking, 32,000 max tokens, no
  fallback model; every call's served model recorded (all 5,159 were Haiku 5.5).
- Problems: the 204 miniF2F test problems we had never run (ranks 41-244 of a seeded ranking);
  Lean 4.24.0, Mathlib `f897ebc`, miniF2F-lean4 `5746b7d`.
- Arms table (B, F, R4, P), all with the identical first request; caps: 4 calls, 1,200 s per
  attempt, 120 s in-loop check, 300 s final check. The arms are matched on the cap, not on calls
  used (F 1.33, R4 1.36, P 1.36 per attempt), tokens or cost.
- Verifier: each final proof goes to a fresh Lean process; it passes only with the statement
  unchanged, no `sorry`, axioms limited to `propext`, `Classical.choice`, `Quot.sound`, within
  300 s.
- Machine: 16 GB of RAM, no swap, 4 CPUs; the study's processes share a 14.3 GB memory limit; at
  most 3 Lean processes and 4 cells at once; time limits are wall-clock, enforced by the process
  running the cells.
- One seeded interleaved order over all 4,080 cells.

### 4. Pre-registration and analysis (0.75 page)
- What was fixed before any paid call: hypotheses, estimand (mean per-problem difference in
  verified rate), the analysis script, the reading rules, the budget and stop rules. Evidence:
  registration commit `64abcd7`, pushed 2026-11-25 before any paid call, with the SHA-256 of
  `REGISTRATION.md` and of every registered file; [whether it was lodged on OSF: owner].
- Tests: two-sided paired sign-flip test, exact on the 1/60 lattice; 10,000-resample percentile
  bootstrap; three-way reading with a ±3-point margin (the 90% interval for H1; Holm and the 95%
  interval for H2-H3); the attribution rule.
- Power: 0.96 at the planning effect of +4.1 points (from the pilot, discounted for first-call
  noise).
- **Deviation D1.** It changed only the run procedure (resuming after the crash), but it was
  committed after 2,202 of 4,080 records, not before the phase as registration section 9
  requires; the first 2,202 records ran under the registered procedure. We report everything D1
  promised (section 6), and the post-phase note on what it got wrong.
- Everything not produced by `analyze.py` run unchanged is exploratory (not registered) and
  changes no reading.

### 5. Results (1.5 pages)
- **Table 1:** H1-H3 with the estimate, 95% and 90% intervals, higher / lower / equal, sign-flip
  p, Holm p, exact sign test p, the comparison arm's unverified share (7.9%), and the reading.
- One sentence on why H1's 95% interval (+0.1 to +2.9 at the registered seed) excludes zero
  although p = 0.093: the registered test decides; the interval's ends depend on the seed, while
  the 90% interval that carries the reading does not.
- **Figure 1** (two panels): registered H1 (90% and 95% intervals), H2 and H3 (95%) with the
  ±3-point band; descriptive contrasts with B (95%), without the band.
- Registered descriptives: contrasts with B (+8.2, +9.6, +8.2); F − R4 by B's rate (139 / 49 / 16
  problems); headroom-scaled F − R4 17% (1% to 38%); post-first-call gain +2.9 (+0.3 to +5.7),
  with the first-call caveat (about +1.9 at equal first-call failures; exploratory); rescue after
  a failed first call (64% F against 52% R4 and P, pooled over attempts, mostly in "other Lean
  error"); first-call parity.
- Exploratory (not registered): the 12 problems that differ in H1, and an influence check (H1 is
  +0.9 points without `mathd_numbertheory_175`, +0.5 without it and `amc12a_2021_p14`; leaving out
  any one problem gives +0.9 to +1.5).
- Exploratory figure: share verified by call number among attempts whose first call failed (F's
  second call verified 100 of 185; R4's and P's 60 of 169 each; calls 3-4 added F 19, R4 28,
  P 28).
- Cost (descriptive): F/R4 cost per verified proof 0.93 (0.86-0.99); calls and tokens by arm.

### 6. Out-of-memory kills on the checking machine (1 page)
- Every final proof of one problem (`decide +kernel` over m below 2020) needed more than the
  14.3 GB available: checked alone, they were killed at 14.0 GB. The Linux out-of-memory killer
  ended 17 of their 20 final checks, in all four arms, and once the whole run (4 cells
  interrupted, re-run on resuming; $3.20 charged).
- Two other final checks were ended by the same events; re-checked alone, both proofs pass (F's
  `aime_1990_p15` in 9.1 s, B's `imo_2019_p1` in 8.5 s). One in-loop check was killed; its cell
  was verified anyway. The registered 300 s timeout fired late in 3 checks during memory stalls.
- Under the registered rule the readings stand. Bounds on the mean differences (at most 0.59
  points).
- **Table 2 (exploratory, not registered):** recounts that count memory-ended cells as passes,
  with the H1 estimate, 90% interval, p and reading, and H2's Holm p and reading. Rows: none;
  `aime_1990_p15` only (the cell known to pass: +1.47, p = 0.062); the smallest recounts that flip
  H1 (it plus any one of F's other killed cells: p = 0.043; any three of those without it:
  p = 0.048); all of F's; all arms; R4's only (+0.98, p = 0.32).
- D1's promised analyses: H1-H3 without `mathd_numbertheory_451` (unchanged); H1 without the 476
  exposed (problem, repetition) pairs (+1.6, 90% +0.1 to +3.3: inconclusive, on 53% of the data);
  exposure by arm.
- The open recount, very likely settled (exploratory): re-checks alone showed 16 of the 17 proofs
  killed again at 14.0 GB and 1 timing out at 300 s. With the memory limit lifted by swap, each of
  F's 5 passed 450 s of user CPU in one thread, 1.5 times the limit, without finishing (memory still
  growing, 25.8-26.5 GB; the paging, counted as system time, left out). The run's CPU was not
  recorded, but in the run 72 ordinary checks took a median of 0.99 times as long as when re-timed
  one at a time on the machine measured: the run's machine was about as fast on ordinary checks, far
  from 1.5 times as fast. So very likely none would have checked within 300 s even with enough
  memory, and the recounts that change a reading are very unlikely; counting the two known artifacts
  as passes, every reading equals the registered one (H1 p = 0.062). Caveats: running under swap may
  inflate user time, and the conclusion fails if it did so by about half (about twofold for the part
  after memory passed 14 GB, where paging became heavy), at the median speed ratio; the speed ratio
  comes from ordinary checks, and carrying it over to this memory-heavy proof is an assumption; a
  first measurement, stopped at 330 s (a 10% margin), was superseded, and a version that counted
  system time was discarded. No check of any proof of this problem has ever passed: 0 of 82 checks
  that reached a verdict, plus 9 measurements of F's 5 proofs stopped unfinished.
- Lessons for harness builders: cap each check's memory, time checks outside the process being
  timed, keep the kernel's evidence, decide in advance what a killed check means.

### 7. From pilot to pre-registered test: an estimate that shrank (0.75 page)
- **Figure 2** (two panels; estimates with 95% intervals; no p-values, no pooled estimate or trend
  line):
  - (a) F − R4: pilot Haiku +5.8 [+0.8, +12.5] (40 reused problems × 3, arms in separate time
    blocks); pilot Opus +5.8 [0.0, +12.5] (F and R4 run a day apart); this study +1.4 [+0.1, +2.9]
    (204 new problems × 5, interleaved);
  - (b) gain over one call: earlier study, Opus, F − B +12.5 [+4.2, +22.5]; pilot Opus R4 − B
    +6.7 [+0.8, +14.2]; pilot Haiku F − B +13.3, R4 − B +7.5; this study F − B +9.6, R4 − B +8.2,
    P − B +8.2.
- Possible reasons the pilot's estimate was larger (not tested): few informative problems (4 of
  40 differed); arms run in separate time blocks, with first-call differences worth about 1.8 of
  the 5.8 points; and selection, since we ran this study because the pilot's estimate was large.
- What it means for our earlier result: that comparison (Opus 5.5, feedback against one call,
  +12.5 points) could not separate feedback from extra attempts, as its report said. On its 40
  problems the pilot's exploratory runs split the gain roughly evenly (fresh tries +6.7 for Opus
  and +7.5 for Haiku over one call; feedback +5.8 over fresh tries for both), with arms not
  interleaved and wide intervals. In this study (Haiku, 204 new problems), fresh tries gave +8.2
  of feedback's +9.6 points over one call (descriptive). We do not re-estimate the earlier result.

### 8. Limitations (0.5 page)
- One model, effort level, prompt and cap of 4 calls; arms matched on that cap, not on calls made,
  tokens or cost; no long agent loops.
- Public problems: contamination may interact with feedback (Lean's report can help repair a
  memorized proof written for an older Lean or Mathlib). "Unknown name" failures were 16 of F's 185
  failed first calls (rescued 11/16 against R4's 6/12).
- The placebo is one sentence.
- "No difference of 3 points or more" concerns the overall verified rate at this cap; it does not
  show that feedback never helps.
- Saturation: B always solved 139 of 204 problems, and only 12 problems differ in H1, so the test
  says little about hard problems. On the 16 problems B never solved, F − R4 was +11 points (−2.5
  to +29; descriptive, too few problems): a hypothesis for a new pre-registered study, not a
  finding.
- The memory events and the late timeouts (section 6).

### 9. Reproducibility and release (0.25 page)
- Released with the paper: code, the registration and its hashes, every record, transcripts of
  prompts and outputs (now in `held_out/artifacts/`, which git ignores: they must be packaged),
  kernel logs, the analysis and re-check scripts.
- Total spend $17.33 from the records, $20.54 including the $3.20 charged for the 4 cells the crash
  interrupted (cap $35).

### 10. Disclosures
- This study was designed, coded, run and analysed, and this paper drafted, with the help of Claude
  (Anthropic); the model under test, Claude Haiku 5.5, is also Anthropic's.
- The independent reviews and recomputations were done by separate Claude agents with their own
  code, not by people.
- Funding and affiliations: [owner].

### Appendices
- A. The registration (verbatim) and the deviation D1 with the post-phase note.
- B. Full tables: all contrasts and diagnostics (`analysis.json`), integrity counts, costs.
- C. Prompts and the placebo sentence.
- D. The out-of-memory kills: kill-to-check matching, exposure tables, re-checks, the CPU-time
  measurement (exploratory).
- E. Independent verification (exploratory): recomputation, record integrity, Lean re-checks.

## Figures and tables to make

| | Content | Source |
|---|---|---|
| Table 1 | H1-H3, with the registered side items | `analysis.json` |
| Figure 1 | forest plot, two panels | `analysis.json` |
| Figure 2 | pilot and earlier study against this study, two panels | the two earlier reports and `analysis.json` |
| Table 2 | recounts (exploratory) | `d1_report.json` |
| Figure 3 (exploratory) | share verified by call number after a failed first call | the records |
| Figure 4 (optional) | the run's timeline with memory events | `oom_kernel_log.txt` |

## Claims the owner must approve before any release

1. The headline: no difference of 3 points or more in the overall verified rate between feedback
   and fresh tries, for Claude Haiku 5.5 at a cap of 4 calls on these problems, with a small
   positive estimate.
2. The pilot's estimate shrank: our 90% interval excludes the pilot's +5.8 and the planned +4.1.
3. Our earlier math result compared feedback with one call and cannot separate feedback from extra
   attempts; this study does not re-estimate it.
4. The account of the memory events, its bounds and recounts.
5. Releasing the paper releases the results; under registration section 12 that commits us to
   release all code, records, transcripts and the analysis with it, whatever they show.
6. Authors, affiliations and funding; the AI-assistance disclosure.
7. The related-work characterizations in `PAPER.md` section 2, each checked against its source
   (`REFERENCES_CHECK.md`), including the correction that pre-registration is not new here: Li and
   Li (2026) pre-registered their primary endpoint, which our registration's background, written
   before that was read, did not know.

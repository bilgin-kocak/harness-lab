# Paper draft: outline and abstract

- **Status: draft for the owner, 2026-11-26. Not published, not submitted.** Every claim below
  needs the owner's approval before it leaves this repository.
- **Sources:** `../RESULTS.md` (the results draft), `../analysis.json` (the registered output),
  `../d1_report.md`, `../REGISTRATION.md`, `../RELATED_WORK.md`, the pilot
  (`../../2026-11-math-pilot/REPORT.md`) and the earlier math study
  (`../../../docs/results/2026-11-23-math.md`). Each number here is taken from one of them.
- **Form:** an arXiv preprint of about 8 pages plus appendices, then a workshop that takes careful
  evaluations or negative results.

## Title options

1. *Feedback or just another try? A pre-registered test of Lean's error report against fresh
   attempts, with Claude Haiku 5.5 on miniF2F*
2. *Does the proof checker's error report help? A pre-registered comparison at a matched call cap*
3. *From pilot to pre-registration: a feedback effect in LLM theorem proving that shrank*

Option 1 is the most accurate; option 3 leads with the method story.

## Abstract (draft, 240 words)

> Showing a language model its failed proof together with the proof checker's error messages is a
> common part of LLM theorem-proving systems, and gains from it are often measured against a single
> call. We ask whether the error report helps beyond simply trying again within the same call
> budget. In a pre-registered study, Claude Haiku 5.5 attempted 204 miniF2F problems in Lean 4,
> five times each, in four interleaved arms: one call (B); up to four calls, each after the first
> showing the failed proof and Lean's report (F); up to four fresh tries (R4); and a placebo arm
> that shows the failed proof with a fixed rejection sentence instead of Lean's report (P). The Lean
> kernel checked every proof. F verified 93.4% of attempts and R4 92.1%, a difference of +1.4
> percentage points (90% CI +0.2 to +2.6; sign-flip p = 0.093), which the registered rule reads as
> no difference of 3 points or more. F against P (+1.4, Holm p = 0.12) and P against R4 (0.0) read
> the same way. All three four-call arms verified 8 to 10 points more than one call, and fresh tries
> alone recovered most of feedback's gain over one call. An exploratory pilot on 40 reused problems
> had suggested a 5.8-point advantage for feedback over fresh tries; it did not replicate. A memory
> failure in the proof checker ended 19 checks during the run; we report it, bound its effect, and
> show which recounts would change the readings. Code, records and analysis are released.

**Words that need care:** "did not replicate" (the pilot's estimate lies outside our 90% interval,
but the pilot was exploratory); "most of feedback's gain" (R4 − B +8.2 against F − B +9.6 points,
descriptive); "released" (the owner decides; the registration commits to releasing everything if
results are released).

## Outline

### 1. Introduction (1 page)
- Repair loops with checker feedback are everywhere in LLM proving; many reported gains compare a
  multi-call loop with one call, which mixes feedback with extra attempts.
- Question: at a matched cap of calls, does Lean's report help, and does it help because of the
  report itself or because the model sees its failed attempt?
- Contributions:
  1. a pre-registered, call-matched comparison with a placebo arm (4,080 attempts, 204 problems,
     5 repetitions, exact tests);
  2. the answer for this setup: no difference of 3 points or more, a small positive estimate, no
     attribution to the report;
  3. a worked case of a pilot effect shrinking under pre-registration, and of our own earlier
     result being mostly extra attempts;
  4. how a verifier infrastructure failure was handled, bounded and reported.

### 2. Related work (0.75 page)
- Call-matched refinement against resampling: Klingner et al. (arXiv 2606.05632), one run per
  problem, 50-problem subsets, 14 models.
- Feedback against resampling with open models on hard miniF2F problems: Li & Li (arXiv 2610.22698):
  no feedback condition established an advantage; our F and P correspond to their conditions.
- Self-repair in code: Olausson et al. (arXiv 2306.09896), gains modest once repair's cost is
  counted.
- Reported repair gains at matched samples: Baldur (Isabelle/HOL), Kimina-Prover (blog, 59 hard
  problems); Goedel-Prover-V2's ablation of error messages without a resampling arm.
- miniF2F near saturation for the strongest systems (Seed-Prover).
- What is new here: pre-registration, a placebo arm, 5 repetitions on 204 problems, a current API
  model. (Not a systematic review; say so.)

### 3. Setup (1 page)
- Model and settings: `claude-haiku-5-5`, effort medium, adaptive thinking, 32,000 max tokens, no
  fallback model; every call's served model recorded (all 5,159 were Haiku 5.5).
- Problems: the 204 miniF2F test problems we had never run (ranks 41-244 of a seeded ranking);
  Lean 4.24.0, Mathlib `f897ebc`, miniF2F-lean4 `5746b7d`.
- Arms table (B, F, R4, P), all with the identical first request; caps: 4 calls, 1,200 s per
  attempt, 120 s in-loop check, 300 s final check.
- Verifier: fresh Lean process, statement unchanged, no `sorry`, axioms limited to `propext`,
  `Classical.choice`, `Quot.sound`.
- One seeded interleaved order over all 4,080 cells.

### 4. Pre-registration and analysis (0.75 page)
- What was fixed before any paid call: hypotheses, estimand (mean per-problem difference in
  verified rate), the analysis script with its hash, the reading rules, the budget and stop rules.
- Tests: two-sided paired sign-flip test, exact on the 1/60 lattice; 10,000-resample percentile
  bootstrap; three-way reading with a ±3-point margin (90% interval for H1, Holm and 95% for H2-H3);
  the attribution rule.
- Power: 0.96 at the planning effect of +4.1 points (from the pilot, discounted for first-call
  noise).
- Deviation D1 (mid-phase, run procedure only), and the post-phase note on what it got wrong.

### 5. Results (1.5 pages)
- **Table 1:** H1-H3 with estimates, intervals, wins/losses/ties, p, Holm p, readings.
- **Figure 1:** forest plot of H1-H3 and the three contrasts with B, with the ±3-point margin.
- The 12 problems that differ in H1; two of them carry 0.9 of the 1.4 points.
- Descriptive: contrasts with B; F − R4 by B's rate (139 / 49 / 16 problems); rescue after a failed
  first call (64% F against 52% R4 and P, mostly in "other Lean error"); first-call parity.
- Cost: F/R4 cost per verified proof 0.93 (0.86-0.99); calls and tokens by arm.

### 6. When the verifier breaks: the memory failure (1 page)
- Proofs of one problem (`decide +kernel` over m < 2020) needed more than 13 GB; the kernel killed
  their checks in all four arms, and once the whole run.
- Two other checks were ended by the same events; re-checked alone, both pass.
- Under the registered rule the readings stand; bounds on the mean differences (at most 0.59
  points); the recounts that would flip H1 (Table 2).
- What settled the open recount: re-checks alone, and the CPU-time measurement with swap
  ([to fill in from `../recheck_heavy_cpu.json`]).
- Lessons for harness builders: cap each check's memory, time checks outside the process being
  timed, keep the kernel's evidence, decide in advance what a killed check means.

### 7. From pilot to confirmation (0.75 page)
- **Figure 2:** the feedback effect across three studies:
  - earlier math study, Claude Opus 5.5, F against one call: +12.5 points (40 problems × 3);
  - exploratory pilot, Haiku 5.5, F against R4: +5.8 (40 reused problems × 3; p = 0.125);
  - this study, F against R4: +1.4 (204 new problems × 5; p = 0.093).
- Why the pilot overstated it: few informative problems (4 of 40), reused problems, first-call
  noise (about 1.8 of the 5.8 points), and choosing the comparison after seeing the pilot.
- What it means for our earlier result: in this study fresh tries recover most of feedback's gain
  over one call; the earlier comparison (a different model) likely measured mostly extra attempts.

### 8. Limitations (0.5 page)
- One model, effort level, prompt and call cap; public problems (possible contamination, which may
  interact with feedback); the placebo is one sentence.
- Saturation: B always solved 139 of 204 problems, and only 12 problems differ in H1, so the test
  says little about hard problems. On the 16 problems B never solved, F − R4 was +11 points
  (−2.5 to +29; descriptive, too few problems): a hypothesis for a new pre-registered study, not a
  finding.
- The registered 300 s timeout fired late in 3 checks during memory stalls.

### 9. Reproducibility and release (0.25 page)
- Released: code, the registration and its hashes, every record, transcripts, kernel logs, the
  analysis and re-check scripts. Total spend $20.54.

### Appendices
- A. The registration (verbatim) and the deviation D1 with the post-phase note.
- B. Full tables: all contrasts and diagnostics (`analysis.json`), integrity counts, costs.
- C. Prompts and the placebo sentence.
- D. The memory failure: kill-to-check matching, exposure tables, re-checks.
- E. Independent verification: recomputation, record integrity, Lean re-checks.

## Figures and tables to make

| | Content | Source |
|---|---|---|
| Table 1 | H1-H3 | `analysis.json` |
| Figure 1 | forest plot of six contrasts with the ±3-point band | `analysis.json` |
| Figure 2 | effect across the three studies | the two earlier reports and `analysis.json` |
| Table 2 | recounts that would change the readings | `d1_report.json` |
| Figure 3 (optional) | the run's timeline with memory events | `oom_kernel_log.txt` |

## Claims the owner must approve before any release

1. The headline: no difference of 3 points or more between feedback and fresh tries for Claude
   Haiku 5.5 at a cap of 4 calls on these problems, with a small positive estimate.
2. The pilot's 5.8-point gap did not replicate.
3. Our earlier math result mostly measured extra attempts (stated as "likely", different model).
4. The memory failure account and its bounds.
5. The release of all code and records.
6. Authors, affiliations and funding (registration section 12); the AI-assistance disclosure.

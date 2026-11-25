# Pre-registration: does Lean's error feedback beat fresh tries? A call-matched test with Claude Haiku 5.5 on miniF2F

- **Status: DRAFT, under review, not yet registered.** The registration will be the commit that
  changes this line to "registered" and fills in section 11's hashes. It is made before any paid call
  of this study. An identical copy may also be lodged on OSF; see section 11.
- **Authors:** [owner to fill in]
- **Calls made:** none.

## 1. Summary

We test whether a language model that proves theorems in Lean does better when a failed proof comes
back with Lean's error messages than when it simply tries again from scratch, with the number of
model calls held equal.

- **Model:** Claude Haiku 5.5.
- **Problems:** 204 miniF2F problems we have never run.
- **Arms:** four, each repeated 5 times per problem, in one interleaved random order.
  - B: one call.
  - F: up to 3 feedback rounds with Lean's errors.
  - R4: up to 4 fresh tries.
  - P: F's rounds, but with a fixed rejection sentence instead of Lean's errors.
- **Primary test:** F against R4, with a two-sided paired sign-flip test.

## 2. Background

- **Earlier result.** In our 2026-11 math study (`docs/results/2026-11-23-math.md`), F verified
  117/120 held-out attempts against the baseline's 102/120 with Claude Opus 5.5. F could make up to
  4 calls and the baseline 1, so that comparison mixed feedback with extra calls.
- **Our exploratory pilot** (`studies/2026-11-math-pilot/REPORT.md`) added R4 on the same 40
  problems. Those 40 had been used before.
  - For Haiku 5.5, F − R4 was +5.8 points [+0.8, +12.5], with 4 problems up, 0 down, and sign test
    p = 0.125.
  - For Opus 5.5 it was +5.8 points [0.0, +12.5], 3 up, 0 down, p = 0.25.
  - Only 13 of the 40 problems differed between any arms.
  - The pilot could not settle the question. It is the reason for this study, and the source of its
    power estimate.
- **Related work** (each source read; details in our pilot notes):
  - Klingner et al. (arXiv 2606.05632) compare refine@32 with pass@32 on 50-problem subsets of
    miniF2F and miniCTX, matched on calls, with one refine run per problem.
  - Li & Li (arXiv 2610.22698; NeurIPS 2026 workshop) find that, on hard miniF2F problems, no
    feedback condition established an advantage over instruction-only resampling for two open
    models.
  - Olausson et al. (arXiv 2306.09896) show that self-repair gains in code generation shrink or
    vanish once its cost is counted.
  - Baldur, Kimina-Prover and Goedel-Prover-V2 report gains from error feedback at matched sample
    counts, or ablations without a matched resampling arm.
- **The gap.** There is no pre-registered, call-matched test with repetitions, a placebo arm and a
  current API model.

## 3. What was seen before this registration

- **The 40 problems** the math study used as held-out, and the **30 dev problems** it searched on.
  Both are seen. The pilot also used the 40.
- **The 204 held-out problems** of this study have never been run by us. Before registration,
  no record, stage plan or document in the repository named any of them; a whole-word search was
  run on 2026-11-25.
- **Exposure to the model.** The problems have been public since 2021 (miniF2F v1). The Lean 4 port
  is public, and so are proofs from other systems. Claude Haiku 5.5's training data runs to June
  2026, so the model may have seen them. All arms share that exposure.
- **No held-out result can have influenced this design.**

## 4. Hypotheses and estimands

The estimand of each contrast is the mean, over the 204 problems, of the per-problem difference in
verified rate between two arms. A problem's verified rate is its verified attempts / 5.

| | Contrast | Question | Role |
|---|---|---|---|
| **H1** | F − R4 | At an equal cap of 4 calls, does showing the try's history (the failed proof plus Lean's report) change the verified rate, compared with fresh tries? | Primary. Two-sided; the pilot suggests F > R4. |
| **H2** | F − P | Does Lean's report matter beyond showing the failed proof with a fixed rejection sentence? | Secondary |
| **H3** | P − R4 | Does showing the failed proof, without Lean's report, change the rate compared with fresh tries? | Secondary |

- **Descriptive only** (no hypothesis): R4 − B, F − B and P − B; cost per verified proof by arm;
  rescue after a failed first call.

## 5. Design

### 5.1 Model and settings

- **Model:** `claude-haiku-5-5` through the Anthropic Messages API.
- **Settings:** effort `medium`, adaptive thinking, `max_tokens` 32,000, automatic prompt caching,
  no fallback model.
- **Served model:** every call's served model is recorded. A different model invalidates the
  attempt.
- **Requests:** each round is a fresh request. Thinking blocks never carry across rounds, and a
  prompt is capped at 12,000 tokens (characters / 4). So no request should reach Haiku's
  100,000-token price tier.
- **Settings shared with earlier studies:** the math study's and the pilot's settings (adapter
  `lean-minif2f` 1), as in `study.yaml`:
  - timeouts: 300 s verifier, 120 s in-loop check, 1,200 s per attempt;
  - at most 12 calls per attempt;
  - at most 3 Lean processes at once, with 4 cells running at once.
- **Toolchain:** Lean `v4.24.0` with Mathlib `f897ebc`, and miniF2F-lean4 at `5746b7d`.

### 5.2 Arms

All four arms send the identical first prompt. A round's answer is the proof in its last ```lean
block. It is checked by Lean in the loop, unless it is the attempt's last round. A pass ends the
attempt.

| Arm | Knobs | Calls | Later rounds show |
|---|---|---|---|
| B (`baseline`) | feedback_rounds 0, retries 1 | 1 | – |
| F (frozen) | feedback_rounds 3 | ≤ 4 | each failed proof, "did not check", and Lean's report (its first 3 errors, the timeout, or the rejection reason); notes on rounds that gave no proof |
| R4 | retries 4 | ≤ 4 | nothing: each try is the first prompt again |
| P | feedback_rounds 3, feedback_content placebo | ≤ 4 | as F, but Lean's report is replaced by the fixed sentence "Lean did not accept this proof." |

- **Fixed frozen configuration.** `held_out.fixed_frozen` fixes F as the frozen configuration. The
  freeze takes it without looking at any results.
- **Placebo text.** The placebo sentence is recorded in the adapter's provenance.

### 5.3 Problems, repetitions and order

- **Held-out problems:** 204, from `split.json`, written by `select_tasks.py`. They are ranks 41–244
  of the math study's seeded hash ranking of the 244 miniF2F test problems; ranks 1–40 were that
  study's held-out set.
- **Repetitions:** 5 per problem per arm, giving 4,080 cells.
- **Order:** one seeded random order over all cells (`held_out.order_seed` 20261125), so the arms
  are interleaved in time. The held-out phase runs once.
- **Dev problems:** the math study's 30 dev problems, already used. They serve only the smoke test
  (3 problems × 4 arms) and a setup check (30 × 4 arms × 1). Nothing is selected on them.

### 5.4 Outcome

- **Verified:** an attempt is verified when the independent verifier accepts its output proof. The
  verifier is a fresh Lean process, with the statement unchanged, no `sorry` and the axioms limited
  to `propext`, `Classical.choice` and `Quot.sound`. This holds whatever the attempt's status, as in
  the math study.
- **Not verified:**
  - refusals;
  - attempts with no proof;
  - served-model mismatches;
  - verifier timeouts.
- **Rerun:** infrastructure failures (`unavailable`: no answer from the API for reasons unrelated
  to the configuration) are run again by the protocol. They are not outcomes.

## 6. Analysis (`analyze.py`, committed with this registration)

### 6.1 Data

- **Inputs:** the held-out records the protocol accepts (one valid record per cell). The analysis
  uses the problems with all 5 repetitions of all 4 arms.
- **Incomplete problems:** if any problem is incomplete (for example after a budget stop), the
  report says how many, and the registered analysis runs on the complete ones.

### 6.2 Primary (H1)

- **Estimate.** The mean of d = rate_F − rate_R4 over the problems, with a 95% percentile bootstrap
  interval over problems (10,000 resamples, seed 20261125).
- **Test.** A two-sided paired sign-flip test of mean d = 0, at α = 0.05: 100,000 seeded sign
  patterns, `harnesslab.study.stats.sign_flip_test`, exact enumeration when 16 or fewer problems
  differ.
- **Reading**, fixed now:
  - **"difference"** if p < 0.05. The direction is the sign of the estimate.
  - **"no difference of 3 points or more"** if p ≥ 0.05 and the 90% bootstrap interval lies within
    (−0.03, +0.03).
  - **"inconclusive"** otherwise.
- The exact sign test on the problems that differ is also reported, as a robustness check.

### 6.3 Secondary (H2, H3)

- Both contrasts use the same estimate, intervals and sign-flip test as the primary.
- Holm's correction is applied across the two, at familywise α = 0.05.
- They are interpreted only with their Holm-adjusted p-values.

### 6.4 Descriptive

- **Contrasts:** R4 − B, F − B and P − B, with intervals and unadjusted sign-flip p-values,
  labelled descriptive.
- **Differing problems:** every problem whose rates differ is listed, for each contrast.

### 6.5 Diagnostics

1. **First-call parity.** F, R4 and P send the identical first prompt. Their round-1 in-loop pass
   rates are compared pairwise with the sign-flip test. A p-value below 0.05 is flagged as
   run-to-run noise or drift of a size that bears on H1–H3.
2. **Rescue after a failed first call.**
   - For F, R4 and P: the share of attempts verified, among those whose round 1 did not pass in
     the loop.
   - Also F − R4 and F − P, with a bootstrap over problems.
   - Descriptive: the conditioning set differs between arms.
3. **Drift.** The verified share per arm in each quarter of the run, by start time.
4. **Integrity.**
   - attempt statuses;
   - refusals;
   - calls that stopped at the output token limit;
   - calls served by another model;
   - harness-error records.

### 6.6 Sensitivity of H1

All are reported. None replaces the primary.

- **Defect list.** Without the 8 problems in `split.json` → `sensitivity_exclusions`. They are
  flagged on a public list of statement defects made for another Lean 4 port.
- **Timeouts.** Without problems where any verdict was a verifier timeout.
- **Sign test.** The exact sign test.

### 6.7 Cost

- **By arm:** total cost, cost per attempt, cost per verified proof and calls per attempt, at the
  list prices in `price_check.md`.
- **F/R4 ratio:** of cost per verified proof, with a bootstrap over problems.
- **Total spend,** across all phases.

### 6.8 What is not done

- **Nothing is pooled with other studies.**
- **No problem is dropped** from the primary analysis. The only exception is incompleteness under
  6.1.
- **No test is added after the data are seen.** An extra analysis may be reported only under the
  label "exploratory (not registered)".

## 7. Sample size and power

- **Problems:** the 204 problems are all that remain unused in the miniF2F test split.
- **Repetitions:** chosen with `power.py`, a synthetic simulation that calls no API.
  - It resamples the pilot's 40 Haiku (R4, F) per-problem outcomes onto 204 problems.
  - Per-problem truths are smoothed with a beta-binomial prior fitted to the pilot.
  - It runs 4,000 studies per row and checks the result with exact random flips.

| True F − R4 effect | Power, 3 repetitions | Power, 5 repetitions |
|---|---|---|
| Pilot's (+5.4 points) | 0.97 | 0.98 |
| Three-quarters (+4.1) | 0.91 | 0.96 |
| Half (+2.7) | 0.75 | 0.86 |
| None (false-positive rate) | 0.05 | 0.05 |

- **Choice:** 5 repetitions, for 0.86 power at half the pilot's effect.
- **Assumptions:** the power figures assume that the 204 problems resemble the pilot's 40 (the same
  split and the same ranking, so they are a random sample of the same pool), and that the true
  effect has the stated size.
- **H2 and H3** have no power claim: the pilot did not measure P.

## 8. Budget, approvals and stop rules

- **Cost.** Projected from the pilot's measured Haiku costs per attempt (B $0.0020, F $0.0037,
  R4 $0.0042; P priced as F):
  - held-out phase: about $14, with a range of about $9–19;
  - smoke test: about $0.05;
  - setup check: about $0.40.
- **Caps** (`study.yaml`):
  - held-out reserve: $30;
  - study cap: $45, all phases together.
- **Approvals.**
  1. After the smoke test, the owner sees the measured cost per attempt and the projected total.
     Nothing further runs until the owner approves.
  2. After the setup check and the freeze, the owner approves the held-out budget once.
- **Stop rules.**
  - A spend watchdog stops the held-out phase if total spend reaches $35.
  - Any stop (cap, watchdog or crash) is resumed only from the recorded cells, never by changing
    the design.
- **Prices.** Re-checked against the live pricing page on the day of the first paid call, and
  recorded in `price_check.md`.
- **Expected run time:** about 14 hours for the held-out phase (estimate from the pilot).

## 9. Deviations

- **Any change after this commit** is a deviation. That includes the spec, the split, the analysis
  script, the adapter or prompts, and the run procedure.
  - Each one is committed, with its reason, to `DEVIATIONS.md` before the phase it affects.
  - Each one is reported with the results.
- **Allowed before the freeze, as deviations:** fixes to defects found by the smoke test or the
  setup check. Examples are a crash, a wrong price, or an arm that does not run as described in
  5.2.
- **Integrity checks.** The freeze and the held-out phase refuse to start if:
  - the held-out split;
  - the sealed spec sections (held_out, objective, stats);
  - the adapter's options or provenance

  changed after the freeze.

## 10. What this study cannot show

- **Results apply to this setup only:** one model, one effort level, one prompt, a 4-call budget,
  and miniF2F problems that are public.
- **Agent loops.** Long agent loops (tens of tool-using turns) are out of scope.
- **Contamination.** It is not separated out: every arm shares it.
- **P's sentence.** P controls for Lean's report only with its one fixed sentence.
- **Untested setups.** "No difference" would mean no difference at this budget, not that feedback
  never helps.

## 11. Files and hashes

The registered files are listed below with their SHA-256 at registration. Code: the commit that
adds this file, on branch `claude/bold-goldberg-ir2hpm` of the Harness Lab repository.

<!-- HASHES -->

The owner may also lodge this file on OSF (osf.io), with an embargo if wanted, before the held-out
phase. That gives the registration an independent timestamp.

## 12. Disclosures

- **AI assistance.** This study was designed, coded and will be run with the help of Claude, a model
  by Anthropic. The models under test are also Anthropic's.
- **Funding and affiliations:** [owner to fill in].
- **Code and records.** All code, records, transcripts of prompts and outputs, and the analysis will
  be released with the results, whatever they show.

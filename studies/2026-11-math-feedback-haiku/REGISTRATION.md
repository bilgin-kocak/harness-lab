# Pre-registration: does showing a failed proof with Lean's report beat fresh tries? Claude Haiku 5.5 on miniF2F, at a cap of 4 calls

- **Status: DRAFT, under review, not yet registered.** The registration will be the commit that
  changes this line to "registered" and fills in section 11's hashes. That commit is made before any
  paid call of this study. Its hash is then recorded in `RUNLOG.md`.
- **Authors:** [owner to fill in]
- **Calls made:** none.

## 1. Summary

We test whether a language model proving theorems in Lean does better when, after a failed attempt,
it is shown that proof with Lean's report, rather than simply trying again from scratch.

- **Model:** Claude Haiku 5.5.
- **Problems:** 204 miniF2F problems we have never run.
- **Arms:** four, each repeated 5 times per problem, in one interleaved random order. All four share
  the same cap of 4 model calls per attempt. The calls, tokens and cost actually used are reported
  by arm.
  - B: one call.
  - F: up to 3 rounds, each showing the failed proof and Lean's report.
  - R4: up to 4 fresh tries.
  - P: F's rounds with a fixed rejection sentence instead of Lean's report.
- **Tests:**
  - Primary: F against R4.
  - Secondary: F against P, which isolates Lean's report; and P against R4.

## 2. Background

- **Our earlier result.** In our 2026-11 math study (`docs/results/2026-11-23-math.md`), F verified
  117/120 held-out attempts against the baseline's 102/120 with Claude Opus 5.5. F could make up to
  4 calls and the baseline 1, so the comparison mixed feedback with extra calls.
- **Our exploratory pilot** (`studies/2026-11-math-pilot/`) ran B, F and R4 with Haiku 5.5, and R4
  with Opus 5.5. It used the same 40 problems, already used once. Its figures are exploratory, not
  results.
  - **Haiku:** F's verified rate was 5.8 points above R4's (4 problems up, 0 down, sign test
    p = 0.125).
  - **Opus:** F was 5.8 points above R4 (3 up, 0 down, p = 0.25).
  - **First-call noise.** About 1.8 of Haiku's 5.8 points came from first-call successes, which F
    and R4 share by design: 103 against 99 of 120 with the identical first request. The arms ran in
    separate time blocks. At equal first-call success, the gap would have been about +4 points.
  - **Few informative problems.** Across both models, only 13 of the 40 problems differed between
    any arms; for Haiku alone, 10.
  - **What the pilot settled.** Nothing. It motivates this study and is the basis of its power
    estimate.
- **Related work** (sources, passages and dates in `RELATED_WORK.md`; not a systematic review):
  - Klingner et al. (arXiv 2606.05632) compare refinement with resampling at an equal number of
    calls. They use 50-problem subsets of miniF2F and miniCTX, 14 models and one refinement run per
    problem.
  - Li & Li (arXiv 2610.22698, NeurIPS 2026 workshop) report that, on difficult miniF2F problems
    with two open models, no tested feedback condition establishes an advantage over
    instruction-only resampling. Showing the failed predecessor reduced success, and adding its
    compiler error recovered the loss on one model.
  - Olausson et al. (arXiv 2306.09896) find that self-repair gains in code generation are often
    modest, and sometimes absent, once the cost of repair is counted. Their main comparison is
    matched on samples.
  - Baldur (Isabelle/HOL; 8+8 against 16 samples) and Kimina-Prover (a blog post, self-reported;
    16+16 against 32 on 59 hard problems) report gains from repair at matched sample counts.
  - Goedel-Prover-V2 reports that removing compiler error messages lowers performance, without a
    matched resampling arm.
- **What is new here.** We found no pre-registered comparison that combines all of these:
  - error feedback against fresh tries at an equal call cap;
  - repeated attempts on each problem;
  - a placebo arm that keeps the failed proof but removes Lean's report.

## 3. What was seen before this registration

- **The 40 problems** the math study used as held-out, and the **30 dev problems** it searched on,
  are seen. The pilot also used the 40.
- **The 204 held-out problems** of this study have never been run by us. Before registration, no
  record, stage plan or document in the repository named any of them (whole-word search,
  2026-11-25). No result on them exists, so none can have influenced this design.
- **Exposure to the model.** The problems have been public since 2021 (miniF2F v1), as have their
  Lean 4 port and proofs by other systems. Claude Haiku 5.5's training data runs to June 2026,
  per Anthropic's models overview read on 2026-11-24 (`RELATED_WORK.md`). So the model may have
  seen them.
  - All arms share that exposure, but its effect need not be neutral: see section 10.

## 4. Hypotheses and estimands

The estimand of each contrast is the mean, over the problems, of the per-problem difference in
verified rate between two arms. A problem's verified rate is its verified attempts over its
matched repetitions (6.1); on a complete run, that is out of 5. This is a contrast in the
**overall verified rate on these problems**. Most of them every arm is expected to solve every
time, so the report gives R4's unverified share next to every reading (6.2).

| | Contrast | Question | Role |
|---|---|---|---|
| **H1** | F − R4 | With the same cap of 4 calls, does showing the try's history (the failed proof and Lean's report) change the verified rate, compared with fresh tries? | Primary. Two-sided; the pilot suggests F > R4. |
| **H2** | F − P | Does Lean's report matter beyond showing the failed proof with a fixed rejection sentence? | Secondary |
| **H3** | P − R4 | Does showing the failed proof without Lean's report change the rate, compared with fresh tries? | Secondary |

- **Descriptive only:** the contrasts and diagnostics of 6.4 and 6.5, and cost (6.7).

## 5. Design

### 5.1 Model and settings

- **Model:** `claude-haiku-5-5` through the Anthropic Messages API.
- **Settings:** effort `medium`, adaptive thinking, `max_tokens` 32,000, automatic prompt caching,
  no fallback model.
- **Served model:** every call's served model is recorded. A different model invalidates the
  attempt (5.4).
- **Requests:** each round is a fresh request. Thinking blocks never carry across rounds, and a
  prompt is capped at 12,000 tokens (characters / 4), so no request should reach Haiku's
  100,000-token price tier.
- **Settings shared with earlier studies:** the math study's and the pilot's (adapter
  `lean-minif2f` 1), as in `study.yaml`:
  - timeouts: 300 s verifier, 120 s in-loop check, 1,200 s per attempt;
  - at most 12 calls per attempt;
  - at most 3 Lean processes at once, with 4 cells running at once.
- **Toolchain:** Lean `v4.24.0` with Mathlib `f897ebc`, and miniF2F-lean4 at `5746b7d`.

### 5.2 Arms

All four arms send the identical first request. A round's answer is the proof in its last ```lean
block. It is checked by Lean in the loop, unless it is the attempt's last round. A pass ends the
attempt.

| Arm | Knobs | Call cap | Later rounds show |
|---|---|---|---|
| B (`baseline`) | feedback_rounds 0, retries 1 | 1 | – |
| F (frozen) | feedback_rounds 3 | 4 | each failed proof, "did not check", and Lean's report (its first 3 errors, the timeout, or the rejection reason); notes on rounds that gave no proof |
| R4 | retries 4 | 4 | nothing: each try is the first request again |
| P | feedback_rounds 3, feedback_content placebo | 4 | as F, but Lean's report is replaced by the fixed sentence "Lean did not accept this proof." |

- **Fixed frozen configuration.** `held_out.fixed_frozen` fixes F as the frozen configuration. The
  freeze takes it without ranking any results.
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
- **Outcomes, not reruns:**
  - attempts stopped by a cap (time, cost or calls);
  - attempts ended by an API error that is not an infrastructure failure;
  - attempts ended by a prover exception.

  These are verified only if their last proof passes the verifier.
- **Not verified:**
  - refusals;
  - attempts with no proof;
  - verifier timeouts;
  - attempts invalidated by a served-model mismatch (they carry no proof).
- **Rerun:** infrastructure failures (`unavailable`: no answer from the API, for reasons unrelated
  to the configuration) are not outcomes. The protocol runs them again.

## 6. Analysis (`analyze.py`, committed with this registration)

**What counts as the result.** The registered results are `analyze.py`'s output. The protocol's own
`results.json` and report (frozen and comparators against the baseline) are descriptive.

**Blinding.** None is needed: outcomes are machine-verified, and `analyze.py` runs unchanged on the
records.

### 6.1 Data and missing cells

1. **Resuming.** A stopped held-out phase is resumed, under section 8's rules, until every cell has
   a valid record.
2. **Matched repetitions.** If cells remain missing, a problem's rates use the repetitions that have
   a valid record in all four arms. Problems with no such repetition are left out and listed. On a
   complete run this uses all 5 repetitions of all 204 problems.
3. **Why this is safe.** The cell order is a seeded random permutation, so which cells are missing
   does not depend on arm or outcome.
4. **Reporting.** The report gives:
   - the number of usable and complete problems;
   - every missing cell;
   - a complete-case analysis (6.6).

### 6.2 Primary (H1)

- **Estimate.** The mean of d = rate_F − rate_R4 over the problems.
- **Interval.** A 95% percentile bootstrap over problems (10,000 resamples, seed 20261125). The 90%
  interval comes from the same resamples.
- **Test.** A two-sided paired sign-flip test of mean d = 0, at α = 0.05.
  - It is computed exactly by counting sign patterns on the 1/60 lattice that rates over 1 to 5
    repetitions lie on (`harnesslab.study.stats.sign_flip_test`, `lattice=60`).
  - If no problem differs, p is taken as 1.
- **Reading**, fixed now:
  - **"difference"** if p < 0.05. The direction is the sign of the estimate.
  - **"no difference of 3 points or more in the overall verified rate"** if p ≥ 0.05 and the 90%
    interval lies within (−0.03, +0.03).
  - **"inconclusive"** otherwise.
- **The 3-point margin** is about half the pilot's observed gap, and roughly the smallest gain that
  would justify F's extra complexity over fresh tries in practice.
- **Reported beside the reading:**
  - R4's unverified share, so that a 3-point margin can be read against the room left to improve;
  - the exact sign test on the problems that differ.

  A disagreement between the two tests changes no reading.

### 6.3 Secondary (H2, H3)

- **Method.** The same estimate, intervals and sign-flip test as the primary. Holm's correction is
  applied across the two, at familywise α = 0.05, with a missing p counted as 1.
- **Reading.** Each secondary takes the same three-way reading as H1, using its Holm-adjusted p.
- **Attribution rule.** A claim that Lean's report itself helps requires H2 "difference". If H1 is
  "difference" and H2 is not, the report says the gain over fresh tries is not attributed to Lean's
  report; it may come from seeing the failed attempt (H3). H2 has no power claim, so "inconclusive"
  is a likely outcome for it.

### 6.4 Descriptive

- **Against the baseline:** R4 − B, F − B and P − B, with intervals and unadjusted sign-flip
  p-values. Every problem whose rates differ is listed.
- **F − R4 within strata of B's verified rate:** 1, between 0 and 1, and 0. B's attempts are
  independent of F's and R4's, so the strata are not selected on their outcomes.
- **Headroom-scaled effect:** (rate_F − rate_R4) / (1 − rate_R4), with a 95% percentile bootstrap
  over problems (10,000, seed 20261125).
- **Post-first-call gain:** F − R4 on each problem's verified rate minus its round-1 pass rate.
  This removes the first-call noise that F and R4 share by design.

### 6.5 Diagnostics

1. **First-call parity.**
   - F, R4 and P send the identical first request. Their round-1 in-loop pass rates are compared
     pairwise (three sign-flip tests, Holm-adjusted).
   - An adjusted p below 0.05 raises a flag, which is reported next to H1–H3 and changes no reading.
2. **Rescue after a failed first call.**
   - For F, R4 and P: the share of attempts verified, among those whose round 1 did not pass in the
     loop. Also F − R4 and F − P, with 95% percentile bootstraps over problems (10,000, seed
     20261125).
   - The conditioning event comes before the arms differ (an identical first request), so the sets
     differ only by chance.
   - This statistic reuses H1's attempts, so it is not a separate test. Pooled over attempts, it
     weighs problems with many first-call failures more.
   - It is also split by why round 1 failed: no proof, timeout, an unknown name (identifier or
     constant), or another Lean error or rejection.
3. **Drift.** The verified share per arm in four equal-count quarters of all valid records, in
   order of start time.
4. **Integrity, by arm, over all valid records:**
   - attempt statuses and stop reasons;
   - harness-error records;
   - refusals;
   - calls stopped at the output token limit;
   - calls served by another model;
   - calls that got no response;
   - attempts invalidated by a served-model mismatch;
   - rounds with context dropped by the 12,000-token cap;
   - in-loop timeouts and verifier timeouts;
   - verified attempts whose verifier needed over 120 s.

   If a per-attempt rate of capped or errored attempts, verifier timeouts, dropped-context rounds,
   in-loop timeouts or harness errors differs between F and R4, or between F and P, by more than 1
   percentage point, the report says so next to H1 or H2.

### 6.6 Sensitivity of H1

All are reported. None replaces the primary.

- **Defect list.** Without the 8 problems in `split.json` → `sensitivity_exclusions`. They are
  flagged on a public list of statement defects made for another Lean 4 port.
- **Timeouts.** Without problems where any verdict was a verifier timeout.
- **Complete cases.** On complete problems only.
- **Sign test.** The exact sign test.

### 6.7 Cost

- **By arm,** over matched repetitions:
  - total cost, cost per attempt and cost per verified proof;
  - calls per attempt and output tokens.
- **F/R4 ratio** of cost per verified proof, with a 95% percentile bootstrap over problems.
- **Prices.** Costs are each attempt's recorded charge, at the prices in `pricing.py`, checked
  against the live page in `price_check.md`. A call of unknown cost is charged its worst case.
- **Total spend** across all phases is reported twice: from the records alone, and with interrupted
  cells' reservations.

### 6.8 What is not done

- **Nothing is pooled with other studies.**
- **No problem is dropped** from the primary analysis, except under 6.1.
- **No test is added after the data are seen.** Any further analysis is reported only as
  "exploratory (not registered)".

## 7. Sample size and power

- **Problems:** the 204 problems are all that remain unused in the miniF2F test split.
- **Repetitions:** chosen with `power.py`, a synthetic simulation that calls no API.
  - It resamples the pilot's 40 Haiku (R4, F) per-problem outcomes onto 204 problems.
  - Per-problem truths are smoothed with a beta-binomial prior, Beta(0.20, 0.02), fitted to the
    pilot. The fit lies at the edge of the fitting grid; a finer search gives about (0.17, 0.017).
  - Power uses a normal approximation to the sign-flip null. As a check, 200 studies per row were
    also tested with 20,000 random sign flips; they agree within simulation error at 5 repetitions.

| True F − R4 effect | Power, 3 repetitions | Power, 5 repetitions |
|---|---|---|
| Pilot's, raw (+5.8 points) | 1.00 | 1.00 |
| Pilot's, smoothed (+5.4 to +5.5) | 0.97 | 0.98 |
| Three-quarters (+4.1), **the planning case** | 0.91 | 0.96 |
| Half (+2.7) | 0.75 | 0.86 |
| None (false-positive rate) | 0.05 | 0.05 |

- **Choice:** 5 repetitions.
- **Why the planning case is three-quarters.** Part of the pilot's gap was first-call noise
  (section 2). At equal first-call success the gap would be about +4 points.
- **Why the pilot report's figures were lower.** It estimated about 0.8–0.9 at the pilot's effect
  and 0.5–0.65 at half, for the exact sign test, which is now a robustness check (6.2).
- **Assumptions:**
  - the 204 problems resemble the pilot's 40 (the same split and ranking);
  - no problem's true effect is negative;
  - attempts are independent;
  - the data are complete.
- **H2 and H3** have no power claim: the pilot did not measure P.

## 8. Budget, approvals and stop rules

- **Cost.** Projected from the pilot's measured Haiku costs per attempt (B $0.0020, F $0.0037,
  R4 $0.0042; P priced as F):
  - held-out phase: about $14, with a range of about $9–19;
  - smoke test: about $0.05;
  - setup check: about $0.40.
- **Cap.** The protocol stops every phase at the study cap: `budget.max_cost_usd`, $35 for all
  phases together. Of that, $30 is a held-out reserve the smoke test and setup check may not spend.
- **Approvals.**
  1. After the smoke test, the owner sees the measured cost per attempt and the projected total.
     Nothing further runs until the owner approves.
  2. After the setup check and the freeze, the owner approves the held-out budget once.
- **Stop rules.**
  - A phase stopped by a crash or an outage is resumed from its recorded cells.
  - A phase stopped by the cap resumes only after the owner approves a higher cap. The new cap is
    recorded in `DEVIATIONS.md` and committed before the run resumes.
  - If the owner does not approve, 6.1 applies.
  - Nothing about the design changes on a resume.
- **Prices.** Re-checked against the live pricing page on the day of the first paid call, and
  recorded in `price_check.md`.
- **Expected run time:** about 14 hours for the held-out phase (an estimate from the pilot).

## 9. Deviations

- **Any change after the registration commit** is a deviation. That includes the spec, the split,
  the analysis script, the adapter or prompts, and the run procedure.
  - Each one is committed, with its reason, to `DEVIATIONS.md` before the phase it affects.
  - Each one is reported with the results.
- **Allowed before the freeze, as deviations:** only changes that make an arm run as section 5.2
  describes. Examples are a crash, a harness error, a wrong price, or a cap that stops an arm from
  making its calls.
- **Not allowed on the basis of smoke or setup outcomes:** changes to the prompts, the placebo
  sentence, the knobs, the timeouts or the caps.
- **Integrity checks.** The freeze and the held-out phase refuse to start if any of these changed
  after the freeze:
  - the held-out split;
  - the sealed spec sections (held_out, objective, stats);
  - the adapter's options or provenance.

## 10. What this study cannot show

- **Results apply to this setup only:** one model, one effort level, one prompt, a cap of 4 calls,
  and miniF2F problems that are public.
- **Agent loops.** Long agent loops (tens of tool-using turns) are out of scope.
- **Matching.** The arms are matched on the call cap, not on calls actually made, tokens or cost.
  Those are reported (6.7).
- **Contamination may interact with the arms.** Lean's report can help repair a memorized proof
  written for an older Lean or Mathlib (for example, an unknown lemma name), and fresh tries cannot
  do that. So the effect on these public problems may differ from that on unseen problems. The
  rescue split by failure class (6.5) shows how much of the effect such failures carry.
- **P's sentence.** P controls for Lean's report only with its one fixed sentence.
- **Untested setups.** "No difference" would mean no difference in the overall verified rate at this
  cap, not that feedback never helps.

## 11. Files and hashes

The registered files are listed below with their SHA-256 at registration. The code is the
registration commit, whose hash `RUNLOG.md` records in the next commit. It is on branch
`claude/bold-goldberg-ir2hpm` of the Harness Lab repository, which is a secondary pointer only.

<!-- HASHES -->

The owner may also lodge this file on OSF (osf.io), with an embargo if wanted, before the held-out
phase. That gives the registration an independent timestamp.

## 12. Disclosures

- **AI assistance.** This study was designed and coded, and will be run, with the help of Claude, a
  model by Anthropic. The models under test are also Anthropic's.
- **Funding and affiliations:** [owner to fill in].
- **Code and records.** If the results are released, all code, records, transcripts of prompts and
  outputs, and the analysis will be released with them, whatever they show.

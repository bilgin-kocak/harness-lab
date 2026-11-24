# Exploratory pilot: fresh tries vs Lean feedback, Haiku 5.5 and Opus 5.5

Written 2026-11-24, before any pilot call. **This is an exploratory pilot, not a pre-registered
study.** It has no search, no freeze and no held-out phase. It runs on 40 problems that the
2026-11 math study already used once. Its numbers are early signals for planning a later study.
They are not results to publish or quote.

## Why

The math study (`docs/results/2026-11-23-math.md`) found that F (`feedback_rounds: 3`) proved more
held-out problems than the baseline with Claude Opus 5.5: 117/120 against 102/120. F can make up
to 4 calls per attempt and the baseline makes 1, so that comparison cannot say how much of the gain
comes from Lean's error messages and how much from extra calls.

R4 (`retries: 4`) makes up to 4 fresh tries. It uses the same first prompt, the same 4-call cap and
the same stopping rule (the first proof that passes the in-loop check), and never shows an error
message. The pilot asks three things:

1. **Opus 5.5.** On the same 40 problems, where does R4 land between the math study's baseline and
   F?
2. **Haiku 5.5.** How do the baseline, F and R4 compare?
3. **Cost.** What does an attempt cost for each model and configuration?

The answers feed the design and budget of a later study on unused problems.

## What runs

| Study | Model | Configurations | Problems | Repetitions |
|---|---|---|---|---|
| `haiku/` | `claude-haiku-5-5` | baseline, F, R4 | the 40 | 3 |
| `opus/` | `claude-opus-5-5` | R4 | the 40 | 3 |

- **Opus baseline and F** come from the math study's held-out records
  (`studies/2026-11-math-minif2f/held_out/attempts.jsonl.gz`, run 2026-11-23). The adapter
  options and prompts are identical. The run time differs (and possibly the machine).
- **Adapter options** are those of the math study (model aside): effort `medium`, adaptive
  thinking, `max_tokens` 32,000, the same timeouts and caps, and no fallback model.
  - A served model other than the configured one invalidates the attempt.
  - A refusal counts as a failed round. Refusals are counted and reported.
- **Split** (`make_split.py`):
  - `dev` is the math study's 40 held-out problems.
  - `held_out` is the math study's 30 dev problems. The pilot never runs them.
  - The 204 miniF2F test problems the math study never used are in neither list. They stay unused
    for a later study.
- **Prices** (`price_check.md`): read from the live pricing page on 2026-11-24.

## Order and approvals

1. Unit tests and the math study's reproduce check pass on this commit.
2. `harnesslab study check` passes for both studies, including the leakage audit.
3. **Smoke test** (paid). Stage `smoke` runs 3 problems at 1 repetition:
   - `mathd_numbertheory_222`: the Opus baseline proved it 3/3;
   - `amc12a_2013_p4` and `aime_1991_p9`: the baseline proved them 0/3 and F 3/3.

   That is Haiku baseline, F and R4 (9 cells) and Opus R4 (3 cells). Then `project_cost.py`
   prints the measured cost per attempt and the projected pilot cost.
4. **Stop.** Show the owner the measured cost per attempt and the projected total. Wait for
   approval.
5. After approval, stage `pilot` runs Haiku first, then Opus. The two never run at the same time:
   each Lean process needs about 5 GB, and each study allows 3.
   - Hard caps: $10 for `haiku/` and $30 for `opus/`, smoke included.
   - The projected spend is about $15–22. Any change to a cap is committed before it takes effect.

## Analysis (fixed now, before any pilot data)

All of it is exploratory, and all of it is reported, whatever the result.

- **Unit.** The unit is the per-problem verified rate: verified attempts / 3. Refusals, caps,
  timeouts and errors count as not verified. Infrastructure failures (`unavailable`) are rerun by
  the protocol and are not outcomes.
- **Contrasts.** Opus: R4 − baseline and F − R4, against the math study's records. Haiku: F −
  baseline, R4 − baseline and F − R4.
- **Statistics for each contrast.**
  - Report the mean difference of per-problem rates.
  - Report a 95% percentile bootstrap interval over problems (10,000 resamples, seed 20261124).
  - Run the exact two-sided sign test on the problems whose rates differ, using the core's
    `harnesslab.study.stats`.
  - List every problem whose rates differ.
- **Decomposition for Opus.** The math study's F − baseline (+0.125) splits into R4 − baseline
  ("extra calls") and F − R4 ("feedback beyond extra calls"). The two parts are reported with
  their intervals.
  - This rests on 40 problems, of which about 8 differ.
  - It also compares runs made on different days.
  - So it is a rough split, not an estimate to quote.
- **Cost, per model and configuration.**
  - Mean cost per attempt, and cost per verified proof (total cost / verified attempts).
  - Calls per attempt, output tokens, token-cap stops, refusals and wall time.
- **Also reported.**
  - How often an R4 attempt repeats an earlier try's proof word for word.
  - Every attempt that stopped at the 32,000-token output limit.

## What it cannot show

- **Not new problems.** The 40 problems are public since 2021 and were used once by the math
  study. The pilot says nothing about new problems.
- **Small sample.** Forty problems and three repetitions leave wide intervals. A null is not
  evidence of no effect.
- **Opus contrasts span two dates.** The Opus contrasts join records made a day apart. A change in
  the model's serving, or in the machine's Lean timing, would show up as an effect.
- **One fixed setup.** One effort level, one prompt, one 4-call budget.

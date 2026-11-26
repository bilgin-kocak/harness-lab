# Run log

Times are UTC.

## Registration

- **Commit:** `64abcd7d920bb8f702f42db50a726fd8b899943d`, pushed to `claude/bold-goldberg-ir2hpm`
  on 2026-11-25.
- **REGISTRATION.md at that commit:** SHA-256
  `6a9a3ed682b77ad4861ec2c453e2ff044c022b6de6802508d157a058cbd754b0`.
- **Paid calls before registration:** none.

## Owner approvals

- 2026-11-25: "yes, run the smoke test". Prices were re-checked first (`price_check.md`).
- 2026-11-25: "yes, run the setup check and freeze", given the smoke costs and projections above.
- 2026-11-25: "yes, start the held-out run", given a projection of $10-14 and the $35 cap.

## Stages

| Stage | Code commit | Started | Command | Result |
|---|---|---|---|---|
| smoke | 5a787d8 | 2026-11-25T08:38:00Z | `harnesslab study search studies/2026-11-math-feedback-haiku/study.yaml --stage smoke --yes` | 12/12 cells, $0.0546; every call served by claude-haiku-5-5; 0 refusals, token-cap stops or harness errors |
| setup | a9398f8 | 2026-11-25T14:10:30Z | `harnesslab study search studies/2026-11-math-feedback-haiku/study.yaml --stage setup --yes` | 120/120 cells, $0.2701, 14:10-14:38; every call served by claude-haiku-5-5; 0 refusals, token-cap stops or harness errors |
| freeze | d3deb77 | 2026-11-25T14:39:02Z | `harnesslab study freeze studies/2026-11-math-feedback-haiku/study.yaml --yes` | F frozen (8fadd8740a41), chosen by held_out.fixed_frozen; freeze.json committed in 61beca4 |
| held-out | e939c64 (+ run scripts) | 2026-11-25T17:17:01Z | `run_heldout.sh` (resumes after outages per section 8) and `snapshot_heldout.sh` (hourly record snapshots) | stopped 2026-11-26T01:23:33Z: the kernel's out-of-memory killer killed the process (exit 137) at 2,202/4,080 valid records, $9.21 recorded plus $3.20 charged for 4 interrupted cells; see below and DEVIATIONS.md D1 |
| held-out, resumed | (the D1 commit) | (see below) | `run_heldout.sh`, now also resuming after exit 137 (D1) | (running) |

## Smoke test check (2026-11-25)

The transcripts show every arm running as REGISTRATION.md 5.2 describes:
- All four arms sent the identical first request (system and message) on each of the 3 problems.
- R4's later requests were identical to its first.
- F's later rounds showed "did not check" and Lean's report.
- P's later rounds showed "did not check" and "Lean did not accept this proof.", never Lean's report.
- B made one call.

`project_cost.py` projects the held-out phase at $18.56 from these 12 cells (two of the three smoke
problems are hard) and at $13.77 from the pilot's costs. Both fit the $30 reserve. The upper bound,
every call at the dearest call measured, is $90.36, above the $35 cap.

The first 4 cells waited about 7 minutes for Lean's one-time self-check on a cold file cache, so the
script's 58 h time estimate is inflated. From the pilot's throughput (360 cells in 73 minutes) and
the warm smoke cells (about 87 s each on mostly hard problems), the held-out phase should take about
14-25 hours.

No deviation.

## Setup check and freeze (2026-11-25)

- **Setup check:** every arm ran on all 30 dev problems. Every call was served by
  claude-haiku-5-5, with no refusals, token-cap stops or harness errors. The setup records select
  nothing. The ranking the freeze command prints is for the record only.
- **Freeze checks against REGISTRATION.md, all passing:**
  - frozen F, chosen by fixed_frozen; comparators R4 and P; baseline B;
  - 5 repetitions, order seed 20261125;
  - the 204 held-out IDs' hash and the prompts hash as registered;
  - the placebo sentence and Haiku prices in provenance.
- **The held-out phase's own checks** (`freeze_problems`): none fail.
- **The cells it would run:** 4,080, 1,020 per arm, interleaved.
- **Projection for the held-out phase:**
  - cost: $10.04 from the smoke and setup costs, or $13.77 from the pilot's;
  - budget: ceiling $35, with $0.32 spent;
  - time: about 16-19 h (setup throughput 4.3 cells/min).
- **Deviations:** none.

## Held-out stop and resume (2026-11-26)

- **01:23:33Z.** The kernel's out-of-memory killer killed the held-out process (exit 137). The
  supervisor stopped, as written, because it resumed only after exit 3.
- **Records at the stop:**
  - 2,202 valid records and 0 harness-error records;
  - every call served by claude-haiku-5-5;
  - $9.2054 recorded, plus 4 interrupted cells charged $0.80096 each.
- **Cause:** proofs of `mathd_numbertheory_451` whose Lean check grew past 12.9 GB. The kernel
  lines and the matching records are in `oom_kernel_log.txt`.
- **Decision:** resume, as section 8 requires. The supervisor now also resumes after exit 137.
  This is DEVIATIONS.md D1, committed before the resume. No outcome by arm was looked at.

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

## Stages

| Stage | Code commit | Started | Command | Result |
|---|---|---|---|---|
| smoke | 5a787d8 | 2026-11-25T08:38:00Z | `harnesslab study search studies/2026-11-math-feedback-haiku/study.yaml --stage smoke --yes` | 12/12 cells, $0.0546; every call served by claude-haiku-5-5; 0 refusals, token-cap stops or harness errors |

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

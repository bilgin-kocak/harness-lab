# Run log

Times are UTC.

## Registration

- **Commit:** `64abcd7d920bb8f702f42db50a726fd8b899943d`, committed 2026-11-25 07:22:20Z and
  pushed to `claude/bold-goldberg-ir2hpm` at 07:22:23Z (the remote-tracking reflog's push entry).
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
| held-out | e939c64 (+ run scripts) | 2026-11-25T17:17:06Z (supervisor started 17:17:01Z) | `run_heldout.sh` (resumes after outages per section 8) and `snapshot_heldout.sh` (hourly record snapshots) | stopped 2026-11-26T01:23:33Z: the kernel's out-of-memory killer killed the process (exit 137) at 2,202/4,080 valid records, $9.21 recorded plus $3.20 charged for 4 interrupted cells; see below and DEVIATIONS.md D1 |
| held-out, resumed | 15f7689 | 2026-11-26T02:15:32Z | `run_heldout.sh` as changed by D1 (run log `held_out_run_log_2.txt`; the first run's is `held_out_run_log_1.txt`) and `snapshot_heldout.sh` | complete 2026-11-26T09:08:30Z in one run (exit 0): 4,080/4,080 valid records, 0 harness errors, all calls served by `claude-haiku-5-5`. Since the resume the kernel had 8 out-of-memory events (02:26, 03:07, 03:48, 04:04, 04:50, 05:46, 07:04, 08:00Z): each killed one Lean process of 13.27-13.67 GB, and two of them (02:26, 04:04) also a small one (184 MB, 167 MB). The held-out process was not killed again. See `oom_kernel_log.txt` and `d1_report.md` |

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
- **Spend charged so far:** $12.73 across all phases (smoke $0.0546, setup $0.2701, held-out
  $9.2054, interrupted $3.2038).
  - At the cost per record so far, the 1,878 cells left cost about $7.85, for about $20.6 in all.
  - Each further kill of the held-out process charges about $3.20. So about 4 more kills would
    reach the $35 cap, and section 8 would apply.
- **Cause:** proofs of `mathd_numbertheory_451` whose Lean check grew to 13.2-13.9 GB of the
  14.3 GB limit (the kernel's kB are KiB). The evidence:
  - the kernel lines and matching records are in `oom_kernel_log.txt`;
  - the full reports are in `held_out_snapshot/kernel_oom_boot_2026-11-25T17-15-47.txt`.
- **The interrupted cells' transcripts at the stop.** A cell that runs again appends to the same
  file, so these sizes mark where its first attempt ends:
  - `held_out/artifacts/mathd_numbertheory_451/59eb7f95143b9fcf/4/transcript.jsonl`, 85,999 bytes
    (its `proof.lean` is 844 bytes, written at 01:19:24);
  - `held_out/artifacts/aime_1999_p11/8fadd8740a416895/4/transcript.jsonl`, 55,263 bytes;
  - `held_out/artifacts/imo_1969_p2/40ca2d6a9288fc1d/4/transcript.jsonl`, 43,272 bytes;
  - `held_out/artifacts/amc12_2001_p5/59eb7f95143b9fcf/2/transcript.jsonl`, 17,659 bytes.
- **What the kill left behind:**
  - no Lean or lake process;
  - two Lean temp directories (`/tmp/harnesslab-lean-gcl536fs`, 01:19:25, and
    `/tmp/harnesslab-lean-msfvl12d`, 01:23:11), whose cleanup never ran.
- **Decision:** resume, as section 8 requires, under DEVIATIONS.md D1, which is committed before
  the resume.
  - D1 was reviewed independently (registration, supervisor script, validity), and its findings
    were applied. The supervisor was tested on synthetic runs (a kernel kill, a manual kill, an
    outage).
  - No outcome by arm was looked at.
- **While the phase runs:** only the supervisor, the snapshot loop and the progress check run in
  this session (D1).
- **To stop the run by hand:** kill the supervisor (its PID is in `<run log>.pid`), then the
  `harnesslab.cli study heldout` process. A `kill -9` of the CLI alone also stops it, since the
  kernel log shows no kill.

## After the held-out phase (2026-11-26)

- **During the resumed phase**, nothing ran in this session beyond the supervisor, the snapshot
  loop and the two-hourly progress checks, which read record counts, spend, served models, the
  kernel's kill lines (times and sizes) and the number of records of `mathd_numbertheory_451`. The
  04:12 check wrongly recorded no kills since the resume (the kernel had 4 events by then); the
  06:08 check corrected it. No outcome by arm was looked at before 09:08:30Z.
- **09:09-09:11.** The snapshot loop was stopped; the records were compressed (raw SHA-256:
  attempts `19d0bd50669ae3605aa06e4b4d6a564801b33db2d8a00ff259efe3e4ddec8436`, reservations
  `4de42ac4abc3fba7bd057646d9d086214c38b1b08080e717b98d91e764dad67d`), checked against the raw
  files, and committed with the run logs and kernel evidence (b1ff975).
- **09:12-09:14.** All 17 registered files matched their SHA-256; `analyze.py` ran unchanged and wrote
  `analysis.json` (54afe9a).
- **09:15-10:05.** Exploratory, not registered: `d1_report.py` (D1's reports); `recheck.py`, Lean
  re-checks of 379 cells of the problems that differ and 240 sampled cells (all 619 verdicts
  repeat); an independent verification by four reviewers with their own code (statistics,
  crash evidence, record integrity, registration), each finding then checked by a second reviewer.
  Their confirmed findings are corrected in 5ea302d and after.
- **09:59-11:10.** The 19 memory-touched final checks re-checked one at a time
  (`recheck.py killed --workers 1 --order light-first`): 2 now pass (`aime_1990_p15` F,
  `imo_2019_p1` B), 17 of `mathd_numbertheory_451` fail again (16 killed by the kernel at 14.0 GB,
  1 times out). Until about 10:20 light commands also ran (`d1_report.py`, edits, a commit,
  progress checks); after that nothing else. `recheck_killed.json`, `recheck_kernel_oom.txt`.
- **11:12-11:42.** A second independent review (three reviewers, each finding checked by another)
  fact-checked the written reports; its confirmed findings are corrected in the next commit.
- **Spend:** no paid call after 09:08:30Z. Total $17.33 in records, $20.54 with the interrupted
  cells, against the $35 cap.
- **Results:** `RESULTS.md`, a draft for the owner. Nothing is published.
- **12:16:57.** The session's machine restarted (a new container; the kernel log from the run is
  gone, but its out-of-memory reports were already saved in the study directory).
- **12:21-13:43.** Owner's go-ahead ("do your step 1"): would F's 4 unsettled proofs of
  `mathd_numbertheory_451` check within 300 s with enough memory? No machine with more than 16 GB
  was available, so a temporary 24 GB swap file was added and `recheck_heavy_cpu.py` measured CPU
  time, one proof at a time. A first version stopped on user plus system time; it was stopped after
  F's repetition 0 and discarded (`recheck_heavy_cpu_v1_discarded.json`), because under swap system
  time includes paging. The second counted user time only: each of the 4 passed 330 s of user CPU
  without finishing (`recheck_heavy_cpu.json`). The independent review of the paper outline
  (about 12:24-12:43) ran during the first version, before the second started at 12:43. The swap was turned off afterwards; the swap file
  (`/swapfile-recheck`, 24 GB) is still on the container's disk.
- **Paper:** `paper/OUTLINE.md`, an outline and abstract for the owner, reviewed independently.
  Nothing is published.
- **13:58-15:59.** A fact-check of the new text found its conclusion stated too firmly: the runs
  stopped at 330 s against a 300 s limit, a margin of about 10% that an inflation of user time by
  memory pressure, or a faster CPU, could void. The text was changed to "very likely", and the swap
  file was turned on again at 14:00 and a longer measurement started: F's repetitions 0-4, to
  450 s of user CPU (`recheck_heavy_cpu_450.json`). None finished: each passed 450 s of user CPU
  in one thread (1,372-1,496 s of wall-clock time each), memory 25.8-26.5 GB and still growing
  when stopped.
  While it ran, only light commands ran in the session (file edits, a commit, progress checks).
  The swap was turned off at 15:59.
- **15:59-16:40.** Was the run's machine faster than the container measured? `recheck_speed.py`
  re-timed, one at a time, 72 of the run's final checks (20 s or more of Lean time, not of
  `mathd_numbertheory_451`, not overlapping a check of it; the rule was written in the script
  before it ran, and the script was committed unchanged in 229b64f before any result was looked
  at) and compared each with its recorded time (`recheck_speed.json`). The run took a median of
  0.99 times as long (10th to 90th percentile 0.93 to 1.13, smallest 0.84); all 72 verdicts
  repeated. While it ran, only light commands ran in the session (file edits, two commits
  (229b64f, b643d72), progress checks).
- **16:42-17:11.** A fact-check of the step 1 text (three checkers: numbers, reasoning, method;
  each finding then judged by two reviewers trying to refute it) reported 13 findings: 8
  confirmed, 3 split, 2 refuted. Corrected in the next commit: the 450 s measurement started at
  14:00, not 13:58; `lean_workers: 3` is a cap, not the run's load (a Lean slot was free when 70
  of the 72 re-timed checks started); "would have needed more than 300 s" had lost its hedge; a
  sentence on the swap threshold read backwards; the speed ratio comes from ordinary checks, so
  carrying it over to this memory-heavy proof is now stated as an assumption; the swap threshold
  ("by half") is now stated at the median speed ratio, with the 6 longest re-timed checks (0.97 to
  1.07); and the 15:59-16:40 entry undercounted the commits. The 2 refuted findings asked to
  combine the smallest per-check ratio with the swap threshold (the reviewers found that spread
  to be noise from the short checks) and to count 5 measurements instead of 9; neither was
  applied.
- **17:4x-18:5x. Paper draft** (owner: "ok, draft the full paper from the outline"). `paper/PAPER.md`
  written from `paper/OUTLINE.md` and the sources it names; `paper/make_figures.py` (standard library,
  SVG) and `paper/build.py` (inlines the registration from git, checked against its SHA-256, and
  `DEVIATIONS.md`; renders HTML with pandoc into the git-ignored `paper/build/`). First draft
  516c835 (17:58). Every citation was located and quoted by one agent and re-checked by a second
  (`paper/REFERENCES_CHECK.md`); the check found that Li and Li (2026) had pre-registered their
  primary endpoint, so the paper no longer counts pre-registration as new. Seven reviewers (numbers
  twice, registration fidelity, claim strength, referee, figures, prompts) reviewed the draft, each
  finding checked by a refuter; the confirmed findings are applied in 6ab2cdc through 519558a. Two
  of them also corrected `RESULTS.md`, `d1_report.md` and the post-phase note in `DEVIATIONS.md`
  (wording only; no record or number changed). No model call of the study was made; nothing is
  published.
- **2026-11-27. Paper v2** (owner: "fresh redraft"). `paper/PAPER.md` rewritten from the outline to
  about eight pages plus appendices (main-text prose 5,470 words against v1's 8,350), keeping every
  verified number, checked citation, figure and review correction of v1; detail moved to the
  appendices. v1 as reviewed is kept in `paper/drafts/PAPER_v1.md`. v2 commit 68bd982. Four reviews
  of v2 (regression against v1, numbers, claims and registration, referee), each finding checked by
  a refuter, confirmed 35 findings (several duplicates across reviews), mostly qualifiers the
  compression had dropped; all are applied (1f40800, 9014ff3, 8a42f64). Two also corrected the
  wording of `RESULTS.md` and `d1_report.md` (the 82 earlier checks include 33 ended by the memory
  events). Final v2 main-text prose: 5,830 words, against v1's 7,710.

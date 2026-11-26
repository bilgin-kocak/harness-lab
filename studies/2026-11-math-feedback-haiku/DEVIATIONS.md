# Deviations from the registration

## D1. Held-out phase: resuming after the kernel killed the run for memory (2026-11-26)

- **When it applies.** D1 was committed before the held-out phase resumed, so it is a deviation in
  the middle of a phase. It applies to the cells run after the resume. The first 2,202 records ran
  under the registered procedure.
- **How it was decided.** No outcome by arm was looked at. The figures below come from task IDs,
  times, durations, exit codes and reservations, never from verdict status by arm or
  configuration.
- **Review.** Three independent reviews (registration, supervisor script, validity) were done
  before the resume, and their findings are applied here.

### What happened

- **The stop.** At 01:23:33Z the kernel's out-of-memory killer killed the held-out process (exit
  137). 2,202 cells had valid records, and none had a harness error.
  - The study's processes share a memory cgroup limited to 14.3 GB (14,338,371,584 bytes), on a
    16 GB machine with no swap.
  - The evidence is in `oom_kernel_log.txt`, with the kernel's lines and the matching records, and
    in `held_out_snapshot/kernel_oom_boot_2026-11-25T17-15-47.txt`, the full report of each event.
- **The cause.** The phase had 10 out-of-memory events. Each time, the first process killed was a
  Lean process of 13.2-13.9 GB, which is 92-97% of the limit. (The kernel's "kB" are KiB.)
  - **9 events** were the final check of a proof of `mathd_numbertheory_451`: the kill times match
    those cells' records.
  - **The 10th** was the same. The cell of that problem in progress had written its proof at
    01:19:24 and its Lean check file at 01:19:25, so its final check was running. The other cells
    in progress were on problems that never caused a kill.
- **In 9 events the kernel killed only that Lean process.** Those 9 cells have records, and they
  count as not verified under the registered verifier rule (a pass needs exit code 0; section
  5.4).
  - 8 records read "lean exited with code 137".
  - 1 record (17:39:59) reads "timeout", but its check ran 487 s against the registered 300 s
    timeout. The verifier's own kill (exit -9) came about 187 s late, just after the kernel's kill
    at 17:39:58, which the held-out process itself had triggered. So under memory pressure the
    held-out process, which times every cell in progress, can stall for minutes.
- **In the 10th event the kernel also killed the held-out process,** and then a Lean process that
  had just started.
  - The 4 cells in progress have no record: `mathd_numbertheory_451`, `aime_1999_p11`,
    `imo_1969_p2` and `amc12_2001_p5`.
  - The store charges each of them its full reservation: $0.80096 each, $3.20 in all.
  - On resuming, they run again. They come first in the seeded order.
- **Lean checks running beside these were slowed.** 2,191 records are on other problems.
  - 373 of them come from cells that overlapped a cell of `mathd_numbertheory_451`.
  - Those 373 had both of the phase's other verifier timeouts (19:33:48 and 01:19:19). The other
    1,818 had none.
  - Their waits for a Lean slot reached 173.6 s, against at most 7.9 s otherwise.
  - They had 13 of the 16 final checks that took more than 60 s.
  - None of these ended in exit 137.
- **`mathd_numbertheory_451`'s cells.** At the stop, 11 of its 20 cells had records. Whether any of
  these proofs would have passed with more memory is not known, and was not checked.

### What is done

1. **The phase resumes, as section 8 says:** "A phase stopped by a crash or an outage is resumed
   from its recorded cells."
   - **The design does not change.** The arms, prompts, verifier rules, timeouts, spec and analysis
     stay as registered. The machine and its memory limit stay the same.
   - **The load on the machine stays as it was.** The 14.3 GB cgroup and the machine's 4 CPUs are
     shared with every shell command of this session, and Lean's time limits are wall-clock.
     - During the first 2,202 records, the kernel's task tables list only the supervisor and
       snapshot loops, their `sleep`, the held-out process and its Lean processes.
     - So while the phase runs, nothing else runs in this session beyond the progress check, which
       reads the records about every 2 hours, as before. That rules out test suites, review agents
       and other scripts.
     - Anything else that has to run is written in RUNLOG.md, with its start and end times, and
       reported with the results.
2. **The run procedure changes,** which is a deviation under section 9. `run_heldout.sh` now works
   as follows:
   - **After exit 137:** if the kernel's log says it killed the held-out process, the script waits
     5 minutes and resumes, as it does after exit 3. Any other exit 137, such as a `kill -9` by
     hand, stops the phase.
   - **Before each run:** the script logs and kills any Lean check left by a killed run, and stops
     the phase if one survives.
     - A Lean check runs in its own process group, and its timeout lives in the held-out process.
       So a check left behind would otherwise run beside the resumed run with no timeout.
     - Section 5.1 allows at most 3 Lean processes at once.
   - **The 3-run rule stays as registered (6.1.1):** only harness errors and `unavailable` count.
     - A cell interrupted by a kill runs again on each resume until it has a record. The limits are
       the 40-run stop and the $35 cap.
     - Each kill charges the cells in progress in full, about $3.20. So about 4 more kills would
       reach the cap, and section 8 would then apply.
   - **Kernel evidence:**
     - After each run, the script copies the kernel's new "Killed process" lines into the run log.
     - `snapshot_heldout.sh` keeps the kernel's full out-of-memory reports every hour, in
       `held_out_snapshot/kernel_oom_boot_<boot time>.txt`, and commits them with the record
       snapshots. The kernel's log does not survive a reboot.
     - The run log is copied into the study directory when the phase ends.
3. **The records stay as they are.** The 9 verdicts that ended in an out-of-memory kill stay "not
   verified". Changing what a killed check means after the fact would change an outcome rule.
4. **Rejected alternatives:**
   - **Shielding the held-out process from the out-of-memory killer.** In a cascade like the 10th
     event, the kernel would then kill other cells' Lean checks instead. That writes wrong "not
     verified" records, where a killed held-out process writes none.
   - **A watchdog that kills large Lean processes earlier, or lower parallelism.** Either one
     changes the memory and time the remaining cells' checks get, compared with the first 2,202
     records.

### What is reported with the results

`analyze.py` stays unchanged. These come in addition to its output:

- **Every Lean check killed for memory, final or in-loop, by arm and by problem.** A check counts
  when a kernel "Killed process ... (lean)" or "... (lake)" line falls between its cell's
  `started_at` and `finished_at`, and:
  - **for the final check,** the kill is within 2 s of the record's `finished_at`, whatever the
    verdict's reason or exit code (137, -9 or "timeout");
  - **for an in-loop check** (B has none: it makes one call), a round's in-loop reason is "lean
    exited with code 137" or "lean exited with code -9".

  A kill that matches no check is listed as unmatched, with the cells in progress at the time. A
  killed process under 1 GB anon-rss is listed separately as a bystander. These counts are shown
  next to section 6.5's in-loop and verifier timeouts, which do not include them.
- **Every interrupted cell,** by arm, with its charge and the kill that caused it (6.7 already asks
  for the charges).
- **Cells that started while a cell of `mathd_numbertheory_451` was running,** meaning from that
  cell's `started_at` to its `finished_at`, or 01:11:38-01:23:33 for the interrupted one.
  - For each arm, exposed and unexposed cells are given: their number, in-loop timeouts, verifier
    timeouts, time-cap stops, and Lean-slot waits over 60 s.
  - The rule uses only each cell's start time, which is fixed before its outcome.
- **Exploratory (not registered) analyses:**
  - **H1, H2 and H3 without `mathd_numbertheory_451`.** The registered timeouts sensitivity (6.6)
    already leaves this problem out of H1, since it has timed-out verdicts.
  - **H1 without every (problem, repetition) that has an exposed cell in any arm,** so that
    repetitions stay matched as in 6.1.
- **How large the effects can be:**
  - The out-of-memory verdicts can change a registered mean difference only through
    `mathd_numbertheory_451`: by at most 1/204, about 0.5 points.
  - The one cell of that problem run again after the kill can change it by at most 1/(5 × 204),
    about 0.1 points. It gets a new attempt where it would most likely have had a "not verified"
    record.

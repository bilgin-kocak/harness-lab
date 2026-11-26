# Deviations from the registration

## D1. Held-out phase: resuming after the kernel killed the run for memory (2026-11-26)

Committed before the held-out phase resumed. Decided without looking at any outcome by arm.

### What happened

- **The stop.** At 01:23:33Z the kernel's out-of-memory killer killed the held-out process (exit
  137). 2,202 cells had valid records. The study's processes share a memory cgroup limited to
  14.3 GB, on a 16 GB machine with no swap. The kernel's lines are in `oom_kernel_log.txt`.
- **The cause.** The phase had 10 out-of-memory events between 17:15Z and the stop. Each time, the
  first process killed was a Lean process that had grown to 12.9-13.6 GB.
  - In 9 events it was the final check of a proof of `mathd_numbertheory_451`: the kill times
    match those cells' records.
  - The 10th was most likely the same: a cell of that problem was in progress, and the other cells
    in progress were on problems that never caused a kill.
- **In 9 events the kernel killed only that Lean process.** Those 9 cells have records, and they
  count as not verified under the registered verifier rule (a pass needs exit code 0).
  - 8 records read "lean exited with code 137".
  - 1 record reads "timeout": its check had passed the 300 s limit before the kill.
- **In the 10th event the kernel also killed the held-out process,** and then a Lean process that
  had just started.
  - The 4 cells in progress have no record. One of them is a cell of `mathd_numbertheory_451`.
  - The store charges a cell with no record its full reservation: $0.80096 each, $3.20 in all.
    On resuming, these cells run again.
- **The problem's cells.** At the stop, 11 of `mathd_numbertheory_451`'s 20 cells had records.
  Whether any of these proofs would have passed with more memory is not known, and was not checked.

### What is done

1. **The phase resumes, as section 8 says:** "A phase stopped by a crash or an outage is resumed
   from its recorded cells."
   - Nothing about the design changes. The arms, prompts, verifier rules, timeouts, spec and
     analysis stay as registered.
   - The machine and its memory limit stay the same. So the rest of the phase runs under the
     conditions of the first 2,202 records.
2. **The run procedure changes,** which is a deviation under section 9. `run_heldout.sh` now
   behaves as follows:
   - **After exit 137** (killed, as here), it waits 5 minutes and resumes, as it does after exit 3.
   - **Its stop rule** counts a run that ended with the cell in progress and no record as a failed
     run, like a harness error. A cell with 3 failed runs and no valid record stops the phase for
     the owner.
   - **After each run,** it copies the kernel's new kill lines into the run log.
3. **The records stay as they are.** The 9 verdicts that ended in an out-of-memory kill stay "not
   verified". Changing what a killed check means after the fact would change an outcome rule.

### What is reported with the results

- **Every verdict whose Lean check was killed for memory,** by arm and by problem, from the kernel
  log and the records.
- **Every interrupted cell,** by arm, with what it was charged (section 6.7 already asks for the
  charges).
- **One analysis the registration does not name:** the primary and secondary estimates without
  `mathd_numbertheory_451`. It is labeled "exploratory (not registered)", and `analyze.py` stays
  unchanged.
- **A small effect of resuming:** a cell whose check killed the held-out process gets a new attempt,
  where it would otherwise have had a "not verified" record. At this stop that is one cell, of
  `mathd_numbertheory_451`.

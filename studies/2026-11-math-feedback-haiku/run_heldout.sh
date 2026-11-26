#!/usr/bin/env bash
# Run the held-out phase under REGISTRATION.md sections 6.1 and 8. Writes nothing but the run log;
# the protocol writes the records.
#
# * Exit 0 with nothing left to start: done.
# * Exit 0 with cells not started (the $35 cap): stop; resuming needs the owner's approval.
# * Exit 3 (a harness error, such as an API outage) or exit 137 (the process was killed, for example
#   by the kernel's out-of-memory killer; DEVIATIONS.md D1): wait 5 minutes and resume. If a cell
#   has failed on 3 runs and has no valid record, stop instead (6.1: it is then recorded as
#   missing). A failed run is a harness-error record or a reservation with no record (the cell was
#   in progress when the process died).
# * Anything else: stop.
# After each run, the kernel's new kill lines are copied into the log ("--- kernel:").
#
# Usage: run_heldout.sh <log file>   (from the repository root, with the pinned toolchain on PATH)
set -u
LOG=$1
SPEC=studies/2026-11-math-feedback-haiku/study.yaml
PY=${PYTHON:-python}
run=0
while true; do
  run=$((run + 1))
  kills_before=$(dmesg 2>/dev/null | grep -c "Killed process")
  echo "=== run $run start $(date -u +%FT%TZ)" >> "$LOG"
  "$PY" -m harnesslab.cli study heldout "$SPEC" --yes >> "$LOG" 2>&1
  code=$?
  echo "=== run $run exit $code $(date -u +%FT%TZ)" >> "$LOG"
  dmesg 2>/dev/null | grep "Killed process" | tail -n +$((kills_before + 1)) \
    | sed 's/^/--- kernel: /' >> "$LOG"
  if [ "$code" -eq 0 ]; then
    summary=$(grep "held-out: ran" "$LOG" | tail -1)
    if echo "$summary" | grep -q "not started 0," && ! echo "$summary" | grep -q "budget"; then
      echo "=== COMPLETE" >> "$LOG"
    else
      echo "=== STOPPED: cells not started (the cap); the owner decides" >> "$LOG"
    fi
    break
  elif [ "$code" -eq 3 ] || [ "$code" -eq 137 ]; then
    strikes=$("$PY" - <<'PY'
import json
from collections import Counter
from pathlib import Path
d = Path("studies/2026-11-math-feedback-haiku/held_out")
def rows(name):
    p = d / name
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
records, reservations = rows("attempts.jsonl"), rows("reservations.jsonl")
key = lambda r: (r["task_id"], r["config_hash"], int(r["repetition"]))
valid = {key(r) for r in records if not r.get("harness_error")}
failed = Counter(key(r) for r in records if r.get("harness_error"))
recorded = {r.get("reservation_id") for r in records}
failed.update(key(e) for e in reservations if e.get("id") not in recorded)
print(sum(1 for k, n in failed.items() if n >= 3 and k not in valid))
PY
)
    if [ -z "$strikes" ]; then
      echo "=== STOPPED: the 3-run check failed; needs attention" >> "$LOG"
      break
    fi
    if [ "$strikes" -gt 0 ]; then
      echo "=== STOPPED: $strikes cell(s) failed on 3 runs (6.1); the owner decides" >> "$LOG"
      break
    fi
    if [ "$run" -ge 40 ]; then
      echo "=== STOPPED: 40 runs; the owner decides" >> "$LOG"
      break
    fi
    if [ "$code" -eq 3 ]; then
      echo "=== harness error: resuming in 5 minutes (section 8)" >> "$LOG"
    else
      echo "=== killed (exit 137): resuming in 5 minutes (section 8; DEVIATIONS.md D1)" >> "$LOG"
    fi
    sleep 300
  else
    echo "=== STOPPED: exit $code; needs attention" >> "$LOG"
    break
  fi
done

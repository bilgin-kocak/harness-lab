#!/usr/bin/env bash
# Run the held-out phase under REGISTRATION.md sections 6.1 and 8. Writes nothing but the run log;
# the protocol writes the records.
#
# * Exit 0 with nothing left to start: done.
# * Exit 0 with cells not started (the $35 cap): stop; resuming needs the owner's approval.
# * Exit 3 (a harness error, such as an API outage): wait 5 minutes and resume. If a cell has 3
#   harness-error records and no valid one, stop instead (6.1: it is then recorded as missing).
# * Anything else: stop.
#
# Usage: run_heldout.sh <log file>   (from the repository root, with the pinned toolchain on PATH)
set -u
LOG=$1
SPEC=studies/2026-11-math-feedback-haiku/study.yaml
PY=${PYTHON:-python}
run=0
while true; do
  run=$((run + 1))
  echo "=== run $run start $(date -u +%FT%TZ)" >> "$LOG"
  "$PY" -m harnesslab.cli study heldout "$SPEC" --yes >> "$LOG" 2>&1
  code=$?
  echo "=== run $run exit $code $(date -u +%FT%TZ)" >> "$LOG"
  if [ "$code" -eq 0 ]; then
    summary=$(grep "held-out: ran" "$LOG" | tail -1)
    if echo "$summary" | grep -q "not started 0," && ! echo "$summary" | grep -q "budget"; then
      echo "=== COMPLETE" >> "$LOG"
    else
      echo "=== STOPPED: cells not started (the cap); the owner decides" >> "$LOG"
    fi
    break
  elif [ "$code" -eq 3 ]; then
    strikes=$("$PY" - <<'PY'
import json
from collections import Counter
from pathlib import Path
p = Path("studies/2026-11-math-feedback-haiku/held_out/attempts.jsonl")
rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
key = lambda r: (r["task_id"], r["config_hash"], int(r["repetition"]))
valid = {key(r) for r in rows if not r.get("harness_error")}
errors = Counter(key(r) for r in rows if r.get("harness_error"))
print(sum(1 for k, n in errors.items() if n >= 3 and k not in valid))
PY
)
    if [ "${strikes:-0}" -gt 0 ]; then
      echo "=== STOPPED: $strikes cell(s) failed on 3 runs (6.1); the owner decides" >> "$LOG"
      break
    fi
    if [ "$run" -ge 40 ]; then
      echo "=== STOPPED: 40 runs; the owner decides" >> "$LOG"
      break
    fi
    echo "=== harness error: resuming in 5 minutes (section 8)" >> "$LOG"
    sleep 300
  else
    echo "=== STOPPED: exit $code; needs attention" >> "$LOG"
    break
  fi
done

#!/usr/bin/env bash
# Run the held-out phase under REGISTRATION.md sections 6.1 and 8, as changed by DEVIATIONS.md D1.
# Writes nothing but the run log (and <log>.pid); the protocol writes the records.
#
# * Exit 0 with nothing left to start: done.
# * Exit 0 with cells not started (the $35 cap): stop; resuming needs the owner's approval.
# * Exit 3 (a harness error, such as an API outage): wait 5 minutes and resume. If a cell has 3
#   harness-error records and no valid one, stop instead (6.1: it is then recorded as missing).
# * Exit 137, when the kernel's log says it killed this run's process (D1): wait 5 minutes and
#   resume. The cells that were in progress have no record and run again. An exit 137 the kernel
#   did not cause (a manual kill -9) stops.
# * Anything else, or 40 runs: stop.
# Before each run, Lean checks left by a killed run are logged and killed: each runs in its own
# process group, so it outlives the runner and loses its timeout. After each run, and before the
# next, the kernel's new "Killed process" lines (later kernel timestamps than the last one
# copied) go into the log.
#
# Usage: run_heldout.sh <log file>   (from the repository root, with the pinned toolchain on PATH)
set -u
LOG=$1
SPEC=studies/2026-11-math-feedback-haiku/study.yaml
PY=${PYTHON:-python}
LEAN_PATTERN='/harnesslab-lean-[^ ]*/Candidate[.]lean'
echo $$ > "$LOG.pid"
kills=

kill_times() {  # the kernel timestamps of its "Killed process" lines, oldest first
  dmesg 2>/dev/null | awk '/Killed process/ { s = $0; sub(/^\[ */, "", s); sub(/\].*/, "", s); print s }'
}

copy_kernel_kills() {  # append "Killed process" lines newer than $kt to the log; keep them in $kills
  kills=$(dmesg 2>/dev/null | awk -v a="$kt" '/Killed process/ {
      s = $0; sub(/^\[ */, "", s); sub(/\].*/, "", s); if (s + 0 > a + 0) print }')
  if [ -n "$kills" ]; then
    echo "$kills" | sed 's/^/--- kernel: /' >> "$LOG"
    kt=$(kill_times | tail -1)
  fi
}

kill_leftover_lean() {  # returns 1 if a leftover Lean process survives
  local left
  left=$(pgrep -f "$LEAN_PATTERN" | tr '\n' ',' | sed 's/,$//')
  [ -z "$left" ] && return 0
  echo "=== Lean processes left by the last run; killing them (D1)" >> "$LOG"
  ps -o pid=,etimes=,rss=,args= -p "$left" 2>/dev/null | sed 's/^/--- leftover: /' >> "$LOG"
  pkill -KILL -f "$LEAN_PATTERN"
  sleep 5
  if pgrep -f "$LEAN_PATTERN" > /dev/null; then
    echo "=== STOPPED: Lean processes from the last run remain; needs attention" >> "$LOG"
    return 1
  fi
  return 0
}

kt=$(kill_times | tail -1)
kt=${kt:-0}
run=0
while true; do
  run=$((run + 1))
  copy_kernel_kills
  kill_leftover_lean || break
  echo "=== run $run start $(date -u +%FT%TZ)" >> "$LOG"
  "$PY" -m harnesslab.cli study heldout "$SPEC" --yes >> "$LOG" 2>&1 &
  cpid=$!
  wait "$cpid"
  code=$?
  echo "=== run $run exit $code $(date -u +%FT%TZ)" >> "$LOG"
  copy_kernel_kills
  if [ "$code" -eq 0 ]; then
    summary=$(grep "held-out: ran" "$LOG" | tail -1)
    if echo "$summary" | grep -q "not started 0," && ! echo "$summary" | grep -q "budget"; then
      echo "=== COMPLETE" >> "$LOG"
    else
      echo "=== STOPPED: cells not started (the cap); the owner decides" >> "$LOG"
    fi
    break
  fi
  if [ "$code" -eq 3 ]; then
    strikes=$("$PY" - 2>> "$LOG" <<'PY'
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
    case "$strikes" in
      '' | *[!0-9]*)
        echo "=== STOPPED: the 3-run check failed; needs attention" >> "$LOG"
        break ;;
    esac
    if [ "$strikes" -gt 0 ]; then
      echo "=== STOPPED: $strikes cell(s) failed on 3 runs (6.1); the owner decides" >> "$LOG"
      break
    fi
    why="harness error: resuming in 5 minutes (section 8)"
  elif [ "$code" -eq 137 ] && echo "$kills" | grep -q "Killed process $cpid (python"; then
    why="killed by the kernel (exit 137): resuming in 5 minutes (section 8; DEVIATIONS.md D1)"
  else
    echo "=== STOPPED: exit $code; needs attention" >> "$LOG"
    break
  fi
  if [ "$run" -ge 40 ]; then
    echo "=== STOPPED: 40 runs; the owner decides" >> "$LOG"
    break
  fi
  echo "=== $why" >> "$LOG"
  sleep 300
done

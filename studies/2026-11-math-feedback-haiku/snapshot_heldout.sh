#!/usr/bin/env bash
# While the held-out phase runs, commit and push a compressed copy of its records every hour, so a
# lost machine loses at most an hour. The copies go to held_out_snapshot/, which the protocol never
# reads. Stops when the run log says COMPLETE or STOPPED.
# Each hour it also keeps the kernel's out-of-memory reports (DEVIATIONS.md D1): every block from
# "invoked oom-killer" to "Killed process", with each event's memory statistics and task table, in
# held_out_snapshot/kernel_oom_boot_<boot time>.txt. Lines already kept stay when the kernel's
# buffer wraps; a reboot starts a new file. The memory cgroup's id is replaced by <id>.
# Usage: snapshot_heldout.sh <run log>   (from the repository root)
set -u
LOG=$1
D=studies/2026-11-math-feedback-haiku
mkdir -p "$D/held_out_snapshot"
keep_kernel_reports() {
  local k
  k="$D/held_out_snapshot/kernel_oom_boot_$(uptime -s | tr ' :' 'T-').txt"
  { [ -f "$k" ] && cat "$k"
    dmesg 2>/dev/null | awk '/invoked oom-killer/ { p = 1 } p { print } /Killed process/ { p = 0 }' \
      | sed -E 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/<id>/g'
  } | awk '!seen[$0]++' > "$k.new" && mv "$k.new" "$k"
}
while true; do
  sleep 3600
  for f in attempts reservations; do
    [ -f "$D/held_out/$f.jsonl" ] && gzip -c -n "$D/held_out/$f.jsonl" > "$D/held_out_snapshot/$f.jsonl.gz"
  done
  keep_kernel_reports
  n=$( [ -f "$D/held_out/attempts.jsonl" ] && wc -l < "$D/held_out/attempts.jsonl" || echo 0 )
  git add "$D/held_out_snapshot" && git commit -q -m "Haiku feedback study: held-out records snapshot ($n records, run in progress)" \
    -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" \
    -m "Claude-Session: https://claude.ai/code/session_01CyuyUub4KQqSZDBGtaMpQr" || true
  for i in 1 2 3 4; do git push -q origin claude/bold-goldberg-ir2hpm && break || sleep $((2 ** i)); done
  grep -qE "^=== (COMPLETE|STOPPED)" "$LOG" && break
done

#!/usr/bin/env bash
# Prepare a fresh machine for the math study (plan part A):
#   1. elan and the pinned Lean toolchain (leanprover/lean4:v4.24.0);
#   2. the Python environment from uv.lock;
#   3. the adapter's own setup, run by `harnesslab study check` because the spec sets `fetch: true`:
#      miniF2F-lean4 at 5746b7d, the Lake project with Mathlib f897ebcf72cd and its prebuilt cache
#      (nothing is compiled), and the Mathlib name index (about 9 minutes). They go to the adapter's
#      cache, ~/.cache/harnesslab/lean (or $XDG_CACHE_HOME/harnesslab/lean), unless
#      HARNESSLAB_MINIF2F_DIR / HARNESSLAB_LEAN_PROJECT point elsewhere.
# Needs the network; about 20 minutes and 8 GB of disk. Reads no API key and makes no paid call.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

if [ ! -x "$HOME/.elan/bin/lake" ]; then
  tmp="$(mktemp -d)"
  curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o "$tmp/elan-init.sh"
  sh "$tmp/elan-init.sh" -y --default-toolchain none --no-modify-path
  rm -rf "$tmp"
fi
export PATH="$HOME/.elan/bin:$PATH"
elan toolchain install leanprover/lean4:v4.24.0

cd "$repo"
uv sync --all-extras --group dev
# Constructs the adapter (fetch: true prepares everything above), then runs the split checks,
# including the leakage audit against the Mathlib index. Exit code 0 means ready.
uv run harnesslab study check studies/2026-11-math-minif2f/study.yaml

echo
echo 'Ready. Run the study with PATH="$HOME/.elan/bin:$PATH"; see studies/2026-11-math-minif2f/RUNBOOK.md.'

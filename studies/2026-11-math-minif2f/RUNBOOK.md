# Running the math study (plan part A)

The study runs Claude Opus 5.5 (`claude-opus-5-5`, fixed for the whole study, no fallbacks) as a
prover on miniF2F. The Lean kernel checks every proof. Every step below is resumable: a cell
that already has a valid record is never run again.

## 0. Prerequisites

- **API key.** `ANTHROPIC_API_KEY` must be set in the environment. Check it without printing it:
  `[ -n "$ANTHROPIC_API_KEY" ] && echo set`. Never print, log or commit the key.
- **Machine.** 4 CPUs, 15 GB RAM and 10 GB of free disk. Each Lean process uses about 5 GB, and
  the spec allows 3 at once (`lean_workers`).
- **Network.** github.com (miniF2F, Mathlib), the Mathlib cache server (`lake exe cache get`)
  and api.anthropic.com.

## 1. Setup (no paid call)

```bash
bash studies/2026-11-math-minif2f/setup.sh
export PATH="$HOME/.elan/bin:$PATH"
```

`harnesslab study check` must end with "no problems found". That includes the leakage audit:
no held-out theorem name or statement appears in the dev data, the prompts or the Mathlib index.

## 2. Smoke test (paid, about $1–5)

Plan A9: the baseline and every knob on, for 3 dev problems, 1 repeat each.

```bash
uv run harnesslab study search studies/2026-11-math-minif2f/study.yaml --stage smoke --yes
uv run python studies/2026-11-math-minif2f/project_cost.py
```

**Stop here.** Show the owner the measured cost per attempt and the projected total, and wait for
approval before any further paid run. The hard cap is $150, and the study must stop and ask
before exceeding it.

## 3. After approval

1. Set `budget.held_out_reserve_usd` in `study.yaml` to 1.25 × the projected held-out cost.
2. If the projection does not fit, apply the A9 drop order to stage 1: F+context48k, then
   F+retries2, then F+planning.
3. Commit these changes before stage 1 runs.

Then run:

```bash
uv run harnesslab study search studies/2026-11-math-minif2f/study.yaml --stage stage1 --yes
uv run harnesslab study search studies/2026-11-math-minif2f/study.yaml --stage combine --yes
uv run harnesslab study search studies/2026-11-math-minif2f/study.yaml --stage confirm --yes
uv run harnesslab study status studies/2026-11-math-minif2f/study.yaml
uv run harnesslab study freeze studies/2026-11-math-minif2f/study.yaml --yes
git add studies/2026-11-math-minif2f/freeze.json && git commit -m "Freeze the math study"
uv run harnesslab study heldout studies/2026-11-math-minif2f/study.yaml --yes
uv run harnesslab study report studies/2026-11-math-minif2f/study.yaml
```

- `combine` and `confirm` run only if the budget, held-out reserve included, covers them. They
  skip themselves otherwise and log why.
- The freeze is written once. The held-out phase refuses to start if anything it depends on
  changed since the freeze.

## 4. Report

Write `docs/results/<date>-math.md` from `results.json`. Use the same fields as the quantum
report, and include:

- the refusal count;
- the timeout count (a cold file cache can push a Lean check towards its 300 s timeout);
- every knob that was not searched, with the reason.

Compress the raw records to `attempts.jsonl.gz` before committing them.

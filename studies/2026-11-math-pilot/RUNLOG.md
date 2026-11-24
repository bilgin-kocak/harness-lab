# Pilot run log

Each stage is run from a clean tree at the commit named, with the pinned Lean 4.24.0 / Mathlib
f897ebc project, the miniF2F-lean4 checkout at 5746b7d and the Mathlib name index the math study
used (index SHA-256 a2c6e670…, 233,022 entries). Times are UTC.

| Stage | Study | Code commit | Started | Command |
|---|---|---|---|---|
| smoke | haiku | c8429b5 | 2026-11-24T06:44:46Z | `harnesslab study search studies/2026-11-math-pilot/haiku/study.yaml --stage smoke --yes` |
| smoke | opus | a39511f | 2026-11-24T06:53:20Z | `harnesslab study search studies/2026-11-math-pilot/opus/study.yaml --stage smoke --yes` |

Smoke result: Haiku 9 attempts $0.0314, Opus 3 attempts $0.2989; every call served by the
configured model; no refusals, token-cap stops or harness errors. `project_cost.py`: pilot stage
about $17 expected, $23–26 pessimistic.

**Owner approval, 2026-11-24:** "yes approve, run the full pilot", given the projection above. The
owner was told the run would stop and ask before total pilot spend (smoke included) passed about
$25. A watchdog stops a stage when the charged total reaches $24.00.

| Stage | Study | Code commit | Started | Command |
|---|---|---|---|---|
| pilot | haiku | e4970af | 2026-11-24T18:01:42Z | `harnesslab study search studies/2026-11-math-pilot/haiku/study.yaml --stage pilot --yes` |

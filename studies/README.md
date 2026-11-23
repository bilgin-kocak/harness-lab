# Studies

Pre-registered experiments run with the study protocol of
[ADR 2026-11-22](../docs/adr/2026-11-22-domain-adapter.md): fixed dev and held-out task lists,
search on dev only, a frozen configuration, then one comparison on the held-out tasks.

| Study | Status | Plan |
| --- | --- | --- |
| `2026-11-math-minif2f` | Split frozen; nothing run | [Plan, part A](../docs/plans/2026-11-22-math-quantum-experiment-plan.md#a-mathematics-lean-4-on-a-minif2f-subset) |
| `2026-11-quantum-qasmbench` | Split frozen; nothing run | [Plan, part B](../docs/plans/2026-11-22-math-quantum-experiment-plan.md#b-quantum-compilation-qiskit-on-qasmbench) |

Each study directory holds `select_tasks.py` and the `split.json` it produced. Later phases add
`dev/` and `held_out/` attempt records, `freeze.json` (written before any held-out task runs),
`results.json` and a reproduce script. Nothing in this directory is a result until a report in
`docs/results/` cites it.

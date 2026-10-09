# Recovery tasks

> **Unreleased.** On `main`; ships in the next release.

An agent that can do a task may still do damage when something goes wrong. The classic case is a
lost acknowledgement: a call that changes something succeeds, but its answer never arrives, and
the agent sees a timeout. Retrying without checking does the change twice: two payments, two
deployments, two records. UndoBench (Sah et al., 2026) found agents that completed 84% of tasks
recovered from such faults in only 47% of runs, and naive retries duplicated effects in 53%.

A **recovery task** is the twin of an ordinary task with a fault injected. Harness Lab grades both
on the final state, as it grades every task, and reports **recovery given normal success**: among
the repetitions in which an agent completes the fault-free task, how often it also completes the
one with the fault. That separates "cannot do the task" from "cannot recover".

```bash
harnesslab run demo-recovery --variants fake-careful,fake-naive,fake-noop   # no keys
harnesslab run demo-recovery --variants claude-default --repetitions 3      # real runs
```

```text
recovery: fake-careful succeeds normally 100% · with a fault 100% · recovers in 100% of the runs that succeed normally (1) · 0.0 duplicate effects per run with a fault
recovery: fake-naive succeeds normally 100% · with a fault 0% · recovers in 0% of the runs that succeed normally (1) · 1.0 duplicate effects per run with a fault
recovery: fake-noop succeeds normally 0% · with a fault 0% · recovers: no run succeeded normally · 0.0 duplicate effects per run with a fault
```

`fake-naive` shows why the conditional number matters: its pass rate without the fault is
perfect, and it still pays an invoice twice when an acknowledgement is lost.

## The demo

`demo-recovery` pays three invoices through a payments service whose command-line client lives in
the repository (`tools/payments.py`, backed by SQLite under the ignored `.payments/`). In
`pay-invoices-lost-ack`, the service processes the second payment but its answer never reaches the
client, which reports a timeout that "may or may not have been processed". The hidden check reads
the service's database (the final state) and its request log (the effect history) and reports
`duplicates`, `missing`, `wrong_amount`, `unknown_invoice`, `state_mismatch` and `exactly_once`. It
passes only when every invoice was paid exactly once, for its amount.

The fake variants run a settle script: `fake-careful` checks for an existing payment before every
retry, and `fake-naive` retries blindly, so it succeeds normally and pays twice under the fault. The
fault comes from the service stub's transport settings; an agent that reads the stub can see how
it works. The demo measures what the agent does, not whether it can spot the setup.

## Writing one

Write the ordinary task first, then its twin with the fault, and link the twin with `fault_of`:

```yaml
id: pay-invoices-lost-ack
fault_of: pay-invoices          # the fault-free twin, a task of the same suite
setup:
  commands:
    - python tools/inject_fault.py --drop-response 2   # however your stub injects its fault
verification:
  command: python .harnesslab_verify/check.py
  score_command: python .harnesslab_verify/check.py --score   # report duplicates and the like
```

- Make the fault deterministic, so repetitions of the twins are comparable.
- Grade the final state and the effect history, not the agent's account of what it did. Report the
  damage as score command metrics, such as `duplicates`. Harness Lab averages `duplicates` over the
  runs with a fault, and summarises every metric per variant.
- Protect the service and its inputs with `protected_paths`, and keep its state in an ignored
  directory, so setup leaves the worktree clean.
- A reference solution that is an action, such as making payments, can name a `command` that the
  fake runner and `suite check` run after its overlay:

```yaml
reference_solution:
  overlay: solution            # adds scripts/settle.py
  partial_overlay: naive
  command: python scripts/settle.py
```

## Reading the results

Repetition *r* of the fault task is paired with repetition *r* of its twin. Per variant:
`nominal_success` and `fault_success` (mean pass rates of the two kinds of task),
`conditional_recovery` over `n_conditioned` paired repetitions, and `duplicate_effects`.
`harnesslab run` and `experiment show` print one line per variant, the dashboard's experiment page
has a Recovery table, and exports carry the report under `recovery`.

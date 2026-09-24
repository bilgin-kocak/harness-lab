# Quickstart

Five minutes, no API keys. The bundled demo suite targets `ledgerlite`, a tiny standard-library
Python package, with three tasks: a bug fix, a feature across two modules, and a refactor with a
byte-identical-output invariant. Hidden tests decide every result.

## 1. Run the demo against two fake agents

```bash
harnesslab doctor
harnesslab run demo --variants fake-reference,fake-noop
```

`fake-reference` applies each task's reference solution. `fake-noop` changes nothing but *claims*
success. The terminal shows the task × variant matrix: the reference passes every task, the no-op
fails every task, because the verifier, not the agent, decides.

```text
                    experiment exp_... — demo
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ task                         ┃ fake-reference ┃ fake-noop ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ fix-month-boundary           │       ✔        │     ✘     │
│ add-tag-budgets              │       ✔        │     ✘     │
│ consolidate-money-formatting │       ✔        │     ✘     │
└──────────────────────────────┴────────────────┴───────────┘
```

## 2. Open the dashboard

```bash
harnesslab serve
```

Open <http://127.0.0.1:8000>. Click the experiment, then any cell of the matrix to see the run:
timeline of tool calls, the diff against the base commit, the verifier's output, and the
reproducibility record. See [The dashboard](../guides/dashboard.md).

## 3. Get an editable copy of everything

```bash
harnesslab init my-lab
cd my-lab
```

`init` copies the demo suite (`suites/demo/`), the sweep templates (`sweeps/`), a starting harness
bundle (`harnesses/baseline/`), the grow templates (`grow/`) and `pricing.example.yaml`. All of it
is yours to edit.

## 4. Check the suite's verifiers

```bash
harnesslab suite check suites/demo/suite.yaml
```

Every verifier must fail on the untouched repository and pass with the reference solution. Run
this after editing any task.

## 5. Try the other features on fake data

```bash
harnesslab sweep run sweeps/demo-fake.yaml    # cheapest verified configuration, seconds
harnesslab grow run grow/demo-fake.yaml       # grow a harness from failures, seconds
harnesslab experiment list
harnesslab grow list
```

## 6. Run a real harness

```bash
harnesslab run suites/demo/suite.yaml --variants claude-default   # needs the claude CLI
harnesslab run suites/demo/suite.yaml --variants codex-default    # needs the codex CLI
```

Then [Run Claude Code](../guides/claude-code.md), [Run Codex](../guides/codex.md), or
[Test your own harness](../guides/your-own-harness.md).

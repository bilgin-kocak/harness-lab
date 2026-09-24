# What the optimizer sees

A grow session's headline guarantee is that **hidden tests never reach the optimizer**. If they
did, the optimizer could encode the answers into the harness and the gate would measure
memorisation, not capability. This page states what the optimizer receives, what it never
receives, and how candidates are checked.

## What it receives

For each iteration, one JSON document (persisted as `context.json` under the version directory so
it can be audited):

- the current bundle's content files, path → text;
- the runner and model the deployed agent uses, and the suite's description;
- for each task in the window: the task prompt, the attempt count, the run's status, outcome and
  verified score, the agent's final message, a compact trace digest (one line per tool call,
  command, message or error with exit codes, durations and short output previews), the diff the
  agent produced, the verifier's stdout and stderr, and the run's `llm_calls`, tokens, wall time
  and reported cost;
- the edit constraints: allowed paths, `max_files`, the byte caps;
- the reasons of up to five recent rejections.

The failure case always comes from the latest run of that task under the *current* bundle, never
from a passing run under a candidate that was later rejected.

## What it never receives

- The content of any injected file. Hidden test sources are never read into the view.
- Hidden file names and stems (`test_hidden_budgets.py`, `test_hidden_budgets`), which are
  replaced by `[hidden-test]` wherever they appear.
- Hidden test identifiers: `test_*` function names and test-class names harvested from the hidden
  sources, also replaced by `[hidden-test]`. A unittest verbose line like
  `test_january_includes_last_day (test_hidden_month_boundary.InclusiveRangeTests) ... FAIL`
  arrives as `[hidden-test] ([hidden-test].[hidden-test]) ... FAIL`: the verdict survives, the
  assertion's name does not.
- Hidden source lines of 24 characters or more, which unittest and pytest tracebacks quote, and
  which are replaced by `[hidden-test-line]`. pytest's `>` and `E` markers are stripped before
  matching.
- Fragments created by truncation: every field is scrubbed before it is capped.
- Names echoed back through rejection reasons: lint messages use the placeholder, and reasons are
  scrubbed before they are stored or shown again.

Task ids are visible in the view (the optimizer needs to know which tasks failed) but may not
appear in a candidate.

## How candidates are checked

Before a candidate runs anywhere, the leak lint rejects it if it:

- deletes a file, edits `harness.yaml`, changes more than `max_files` files, or exceeds the size
  caps;
- adds a path outside the allowed set, or a skill without frontmatter, or malformed `hooks.json`;
- mentions a suite task id as a whole word (except in `fake.yaml`, which real runners ignore);
- mentions a hidden file name, stem or test identifier;
- contains a line of 24 characters or more that appears verbatim in a hidden test.

A rejected candidate is recorded as `invalid` with its reason, costs the window tasks an attempt,
and counts toward the three consecutive failures that stop a session.

## Verifying it yourself

`harnesslab harness check <bundle> --suite <suite>` runs the same lint on any bundle. The test
suite runs a whole fake grow session and asserts that no hidden line or hidden name appears in any
`context.json` or `proposal.json` it produced.

## What this does not cover

The verifier's stdout and stderr are shown to the optimizer after scrubbing. Test *counts*,
assertion messages you wrote yourself, and expected-versus-actual values printed by an assertion
can still appear if they do not coincide with a hidden source line. If a hidden assertion prints
the expected answer, the optimizer can see the answer. Write hidden tests so that their failure
output describes the behaviour, not the solution.

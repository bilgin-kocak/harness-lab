# What the optimizer sees

> **Requires Harness Lab 0.2.0 (unreleased).** `pip install harnesslab` currently installs 0.1.0,
> which has no `grow` command and no harness bundles. Until 0.2.0 is on PyPI, install from `main`:
> `pip install git+https://github.com/bilgin-kocak/harness-lab`.

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
  agent produced, the verifier's stdout and stderr with assertion details removed (see below),
  and the run's `llm_calls`, tokens, wall time and reported cost;
- the edit constraints: allowed paths (`hooks.json` only with `optimizer.allow_hooks`),
  `max_files`, the byte caps;
- the reasons of up to five recent rejections.

The failure case always comes from the latest *failing* run of that task under the current
bundle (or, if it has none there, the latest failing run under any bundle), never from a passing
run, including a passing repetition when `repetitions` is above 1.

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
- Assertion details in the verifier's output, unless the grow spec sets
  `optimizer.verifier_detail: full`. unittest's `AssertionError:` payload and the diff lines that
  follow it (`- `, `+ `, `First differing element ...`), pytest's `E   assert ...` blocks and the
  payload of its `FAILED ... - assert ...` summary lines become
  `AssertionError: [hidden-assertion-detail]`. The optimizer still sees which runs failed, error
  types such as `ModuleNotFoundError` or `TypeError`, and tracebacks.
- Fragments created by truncation: every field is scrubbed before it is capped.
- Names echoed back through rejection reasons: lint messages use the placeholder, and reasons are
  scrubbed before they are stored or shown again.

Task ids are visible in the view (the optimizer needs to know which tasks failed) but may not
appear in a candidate.

## How candidates are checked

Before a candidate runs anywhere, the leak lint rejects it if it:

- deletes a file, edits `harness.yaml`, changes more than `max_files` files, or exceeds the size
  caps;
- adds or edits `hooks.json` while `optimizer.allow_hooks` is false (hook commands run on your
  machine outside the agent's tool allowlist);
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

The assertion scrub recognises unittest and pytest output. Expected values printed some other
way (a custom test runner, `print` calls in a hidden test, a doctest, an exception message such as
`ValueError(f"expected {x}")`) are not recognised and reach the optimizer, as does everything when
`verifier_detail: full` is set. Test *counts* always appear. Write hidden tests so that their
failure output describes the behaviour, not the solution.

The lint checks a candidate's *text* for hidden names and lines. It cannot tell whether a skill
encodes an answer in other words, which is why acceptance also requires the held-out gate not to
regress and why the final holdout comparison exists.

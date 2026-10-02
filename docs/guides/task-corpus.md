# Mine tasks from git history

> **Requires Harness Lab 0.2.0 (unreleased).** `pip install harnesslab` currently installs 0.1.0,
> which has no `suite mine` command. Until 0.2.0 is on PyPI, install from `main`:
> `pip install git+https://github.com/bilgin-kocak/harness-lab`.

Verdicts need tasks. A paired comparison says nothing below five tasks, and an ablation, a sweep
or a grow gate on three tasks is mostly noise. Writing tasks by hand is slow. Your repository's
history already holds the material, though: every commit that fixed a bug or added a feature
*together with its tests* is a ready-made task. `harnesslab suite mine` turns such commits into a
suite, the way SWE-bench builds its instances.

## Mine a repository

```bash
harnesslab suite mine ../myproject --out suites/myproject-mined
harnesslab suite check suites/myproject-mined/suite.yaml
harnesslab run suites/myproject-mined/suite.yaml --variants fake-reference,fake-noop
```

The source repository is only read: the miner never writes to it, and it validates candidates in a
temporary clone that it deletes afterwards.

## How a commit becomes a task

For each non-merge commit `C` with parent `P`, newest first:

| Task part | Taken from |
| --- | --- |
| Starting state (`repo.base_ref`) | the parent commit `P` |
| Hidden verifier | `C`'s changed test files, injected at their original paths at verification time and listed in `protected_paths` |
| Verifier command | `--test-command`, with `{tests}` replaced by the test modules (`test_*.py`, `*_test.py`; otherwise all changed test files) |
| Reference solution | `C`'s version of the changed non-test files (an overlay) |
| Prompt | `C`'s commit message without trailers (`Signed-off-by:`, `Co-Authored-By:` …), with hidden test names and file names replaced by `[hidden-test]` |
| Tags | `mined`, a size bucket (`small` ≤ 30 changed source lines, `medium` ≤ 150, `large`), and the top-level source areas (`src/pkg/x.py` → `pkg`) |

Commits are skipped when an overlay cannot express them or they are too large:

- no changed test file, or no changed source file (files matching `--ignore-glob`, such as docs,
  do not count as source but still ride along in the reference solution);
- a source or test file is deleted or renamed, or a changed file is a symlink or submodule;
- more than `--max-files` (6) source files or `--max-lines` (400) changed source lines.

Then **validation** runs the fail-to-pass check: with the hidden tests injected, the test command
must **fail at `P`** and **pass at `C`**. A commit whose new tests already pass at the parent (a
refactor) does not test the change; one whose tests fail at the commit is broken or depends on
something the overlay misses. Both are rejected. `--no-validate` skips this step, which is faster
but keeps such commits. Run `suite check` before relying on an unvalidated suite.

Example output (the test suite's scripted history):

```
Mining calcproj (HEAD, up to 200 commits, validating fail-to-pass)
  rejected 0de8de8ee2 Add multiplication [wip]  fails after the change
  rejected 7749a8901c Tidy add  passes before the change
  kept 1431332251 Add division
  kept 34e29caa12 Fix subtraction returning the sum

2 task(s) kept from 8 commit(s) scanned
  not kept because            commits
  deletes or renames files          1
  fails after the change            1
  no source changes                 1
  no test changes                   1
  passes before the change          1
  root commit                       1
```

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--out`, `-o` | required | Output directory. Refuses to replace a previous suite unless `--force` is given, and then replaces only `suite.yaml`, `tasks/` and `mining_report.json`. |
| `--rev` | `HEAD` | Mine commits reachable from this ref. |
| `--max-commits` | 200 | Newest non-merge commits to scan. |
| `--max-tasks` | none | Stop once this many tasks are kept. |
| `--test-command` | `python -m pytest -q {tests}` | The verifier. Use `python -m unittest {tests}` for unittest projects, or any command; it runs from the repository root. |
| `--test-glob` | `tests/**`, `test/**`, `**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/conftest.py` | Which files are tests (repeatable; replaces the defaults). |
| `--ignore-glob` | docs, `*.md`, `*.rst`, `*.txt`, `.github/**`, changelogs, licenses | Files that are neither tests nor source (repeatable; replaces the defaults). |
| `--max-files`, `--max-lines` | 6, 400 | Size limits on the source change. |
| `--setup` | none | Setup command (repeatable), e.g. `pip install -e .`. It is written into every task's `setup.commands` and run before validation. Setup must leave the worktree clean. |
| `--prompt-template` | built in | A text file with a `{message}` placeholder. |
| `--validate/--no-validate` | validate | Run the fail-to-pass check. |
| `--timeout` | 300 | Seconds per setup or test command (also the tasks' verifier timeout). |
| `--parallelism`, `-p` | 4 | Commits validated at once. |
| `--name` | `<repo>-mined` | Suite name. |

## What gets written

```text
suites/myproject-mined/
  suite.yaml                     the tasks plus fake-reference and fake-noop variants
  mining_report.json             every scanned commit: kept, skipped or rejected, with the reason,
                                 exit codes and durations at the parent and at the commit
  tasks/<repo>-<sha>.yaml        one task per kept commit
  tasks/<repo>-<sha>/verify/…    hidden tests, at their repository paths
  tasks/<repo>-<sha>/solution/…  reference solution overlay
```

`repo.path` points back to the source repository (relative to the task file), so keep the suite
next to the checkout or mine again on another machine. Add your real variants to `suite.yaml`, or
reference the suite from an experiment, sweep or grow file as usual.

## The agent cannot read the answer from history

A mined task starts at `P`, but the source repository also contains `C`, the answer. When an
experiment snapshots a git repository, Harness Lab fetches **only the base commit and its
ancestors** into its internal clone: no later commits, no other branches, no tags, no remote. An
agent that runs `git log --all` in its worktree sees the history up to `P` and nothing after it.
Each base commit gets its own internal clone under `~/.harnesslab/repos/`.

## Caveats

- Validation and verification execute the repository's own tests (and `--setup` commands) on your
  machine. Mine only repositories you trust; the [security model](../explanations/security-model.md)
  applies.
- The prompt is only as good as the commit message. "fix" or "wip" make poor tasks; read the kept
  prompts and drop tasks whose message does not describe the change (delete the task's line from
  `suite.yaml`), or pass `--prompt-template` to add project context.
- Tests that need services, network or a built environment fail at both commits and are rejected
  as "fails after the change"; give `--setup` what they need or filter with `--test-glob`.
- Mined tasks resemble your repository's past, which is what makes them useful for tuning a
  harness on *your* code, and also why results on them say little about other projects. Keep a
  held-out split (grow) or `holdout_tasks` (sweeps) when you tune on them.

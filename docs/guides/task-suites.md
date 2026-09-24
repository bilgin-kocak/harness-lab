# Write a task suite

Run `harnesslab init my-lab` to get an editable copy of the demo suite and start from it. A suite
is a YAML file listing task files and, optionally, variants and plugin modules.

```yaml
# suites/my-suite/suite.yaml
name: my-suite
description: Three tasks against the widget service.
plugins: []                    # python modules registering custom runners or optimizers
tasks:
  - tasks/add-health-endpoint.yaml
variants:
  - id: claude-default
    runner: claude
    max_turns: 30
```

## A task

```yaml
# suites/my-suite/tasks/add-health-endpoint.yaml
id: add-health-endpoint
name: Add health endpoint
version: 1
description: A small feature with a hidden contract test.

repo:
  path: ../fixture_repo      # plain directory (materialized deterministically) or a git repo
  base_ref: main             # only used for git repositories

prompt: |
  Add a health-check function according to the repository requirements.
  Do not modify the tests. Run `python -m unittest discover -s tests` to check your work.

setup:
  commands: []               # run before the agent; must leave the worktree clean
  timeout_seconds: 120

verification:
  command: python -m unittest discover -s tests -v     # exit code 0 = pass
  score_command: python .harnesslab_verify/score.py    # optional partial score
  timeout_seconds: 60
  inject:                    # copied into the worktree only when the verifier runs
    - source: add-health-endpoint/verify/test_hidden.py
      dest: tests/test_hidden.py
    - source: add-health-endpoint/verify/score.py
      dest: .harnesslab_verify/score.py
  protected_paths:           # changes here fail the run before the verifier runs
    - tests/

limits:
  agent_timeout_seconds: 600

tags: [python, feature]

reference_solution:          # optional: used by the fake runner and `suite check`
  overlay: add-health-endpoint/solution
  description: One function plus one route.
```

Paths are relative to the task file. Every field is listed in
[Task and suite YAML](../reference/task-and-suite-format.md).

## The verifier is the contract

- `verified_pass` always comes from the exit code of `verification.command`. Keep agent-specific
  instructions out of it; the verifier is what "done" means.
- **Hidden tests** live next to the task (`tasks/<task>/verify/`) and are copied into the
  worktree only at verification time. An agent that edits visible tests under `protected_paths`
  fails before the verifier runs.
- A **partial score** command must write JSON to the file named by `$HARNESSLAB_SCORE_FILE` (or
  print it as the last JSON object on stdout):

  ```json
  {"score": 0.8, "max_score": 1.0, "metrics": {"tests_passed": 8, "tests_total": 10}}
  ```

  The score only refines `verified_score`; pass or fail still comes from the command's exit code.
- The verifier runs with no provider credentials in its environment.

## Fixture repositories

`repo.path` can be a plain directory or a git repository.

- A **plain directory** is materialized into an internal git repository with a fixed author, date
  and config, keyed by a content hash, so the base commit SHA is identical on every machine for
  identical content. Caches and editor files (`__pycache__`, `.DS_Store`, `.venv`,
  `node_modules`, and so on) are excluded.
- A **git repository** has `base_ref` resolved to a commit and is cloned into Harness Lab's home
  with its `origin` remote removed, so nothing an agent does, not even `git push`, can reach the
  source.

Worktrees are always created from the internal copy, never from your checkout.

## Check the suite

```bash
harnesslab suite list suites/my-suite/suite.yaml
harnesslab suite check suites/my-suite/suite.yaml
```

`suite check` runs the fake runner twice per task: with the reference solution, which must pass,
and on the untouched repository, which must fail. A verifier that passes on the untouched
repository is a broken task. Run it after every edit to a task, its hidden tests or its solution.

## Tips for good tasks

- Make the prompt describe the requirement, not the hidden test. If the prompt names the test,
  the task measures reading comprehension.
- Keep hidden tests independent of the reference solution's exact code. Test behaviour and
  invariants, and use golden outputs where byte-identical output is the point.
- Give each task a `reference_solution` so `suite check` and the fake runner can prove the
  verifier works.
- Use `tags` to group tasks; sweeps can report one recommendation per tag.
- Keep `agent_timeout_seconds` realistic for the slowest harness you plan to compare.

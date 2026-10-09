# Find-everything tasks

> **Unreleased.** On `main`; ships in the next release.

A correct answer is not a complete one. An agent asked for every call of a deprecated function can
list ten real call sites and still miss the five that will break the build when the function is
removed. Most coding tasks cannot see that difference: a test suite checks that what was done is
right, not that nothing was left out.

A **find-everything task** grades the agent's findings against a hidden answer key, with three F1
scores taken from ATLAS (Exa, 2026), a benchmark of exhaustive search:

| Score | Counts | Low when |
| --- | --- | --- |
| **discovery F1** | entities | the agent missed entities or listed ones that are not in the key |
| **item F1** | cells: each entity and each of its graded attributes | entities were found but described wrongly |
| **row F1** | whole rows, correct only when every graded attribute is right | anything about a row is wrong |

Each is the harmonic mean of precision (what share of what the agent claimed is right; a repeated or
unknown entity is a false claim) and recall (what share of the key it found). An agent that claims
nothing has no precision, and an F1 of 0.

```bash
harnesslab run demo-discovery --variants fake-reference,fake-partial,fake-noop   # no keys
harnesslab run demo-discovery --variants claude-default                         # real runs
```

## Writing one

The agent writes its findings to a file, and the task names the answer key and the fields that
identify an entity:

```yaml
prompt: |
  Write findings.json: a JSON list with one object per call of shop.compat.legacy_price, with
  `file`, `line` and `function`. ...
verification:
  command: python -c "import json; json.load(open('findings.json'))"
  answer_key:
    key: find-legacy-price-calls/answer.json   # task-relative; never copied into the worktree
    findings: findings.json                     # the file the agent writes (JSON list or CSV)
    id: [file, line]                            # what identifies an entity
    fields: [function]                          # graded attributes (default: every other column)
    score: row_f1                               # becomes verified_score (row_f1, item_f1, discovery_f1)
    pass_threshold: 1.0                         # the run passes when the command passes and score >= this
    ignore_case: false
  protected_paths: [shop/, tests/]
```

The key is a JSON list of objects or a CSV file with a header, one row per entity. A `null` value in
the key is not graded, for facts nobody could settle. Values compare as text after trimming.
Numbers compare by their exact value, so `12` matches `"12"` and `12.5` matches `"12.50"`, while
`37.7749` does not match `37.774929`. Text counts as a number only when it is written like one:
`"007"` and `"02134"` stay text, so codes keep their leading zeros. Note that version strings such
as `"1.10"` and `"1.1"` look like numbers and match; give them a prefix (`v1.10`) to keep them
apart. `true` and `false` match in any case.

A missing, unreadable or malformed findings file scores 0, and so does a findings file that is a
link: by the time the key is graded the hidden files are in the worktree. A key that cannot be
read, repeats an id or lacks one of the `fields` makes the run `not_verified`. `answer_key` and
`score_command` cannot be combined: each sets the score.

Good find-everything tasks have non-obvious members: an aliased import, a call through a module, a
generated name. They also have decoys: a different function with the same name, a mention in a
comment. The bundled `demo-discovery` task has both. Its partial reference answer is what a text
search finds: it misses the aliased call, includes the unrelated function, and names a method
without its class, so it scores discovery F1 0.86 and row F1 0.71.

## Reading the results

The three scores, their precision and recall, and the counts (`n_key`, `n_predicted`, `n_found`,
`n_rows_correct`) are recorded per run as task metrics. `experiment show`, the dashboard's experiment
page and exports summarise every task metric per variant, so the comparison between two harnesses
shows whether one finds more or only describes better. Pass rate and score comparisons use the
chosen `score`; see [Read and export results](results.md#verdict-metric).

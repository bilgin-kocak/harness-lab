# Study `2026-11-math-minif2f`: tables from results.json

Generated 2026-11-23T15:30:22+00:00 by harnesslab 0.3.0.

## Freeze

- frozen at 2026-11-23T14:30:39+00:00, freeze_sha256 `ad17dec0891d68d992d56595ce4e0ee9196c26933c7b59b03543f6747df8a879`
- git head at the freeze `2a1b60f473ec7676faba067752b05d99671fae4b`, spec sha256 `bcf30d2aa6087d3a3f80c02068f6e8e277db94c4ef7b97f7c4d27e93b779a9ce`
- baseline `980e935d0d7e`: `{'context_limit_tokens': 12000, 'feedback_rounds': 0, 'planning': False, 'retries': 1, 'retrieval': 'none', 'tactic_tools': 'none'}`
- frozen `F` `37f78cb9d35f`: `{'context_limit_tokens': 12000, 'feedback_rounds': 3, 'planning': False, 'retries': 1, 'retrieval': 'none', 'tactic_tools': 'none'}`

## Search (dev)

- records 330, configurations 7, per stage {'stage1': 210, 'confirm': 120}
- cost $13.93, cell time 1.209e+04 s
- verdicts {'pass': 321, 'fail': 9}
- considered tasks 30; left out: none
- feasible configurations 2

| Rank | Label | Feasible | Pass rate | Ratios | Medians | Note |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | F | yes | 0.9889 | – | – |  |
| 2 | baseline | yes | 0.9222 | – | – |  |
| – | F+planning | no | 1 | – | – | not eligible: the freeze considers the confirmed configurations only |
| – | F+retrieval | no | 1 | – | – | not eligible: the freeze considers the confirmed configurations only |
| – | F+retries2 | no | 1 | – | – | not eligible: the freeze considers the confirmed configurations only |
| – | F+tactics | no | 1 | – | – | not eligible: the freeze considers the confirmed configurations only |
| – | F+context48k | no | 0.9667 | – | – | not eligible: the freeze considers the confirmed configurations only |

## Held-out

- complete: yes (240 of 240 planned cells recorded)

| Configuration | Records | Passes | Verdicts | Attempt status | Cost $ | Wall time median s |
| --- | --- | --- | --- | --- | --- | --- |
| F | 120 | 117 | {'pass': 117, 'fail': 3} | {'completed': 120} | 13.2 | 15.39 |
| baseline | 120 | 102 | {'pass': 102, 'fail': 18} | {'completed': 120} | 6.525 | 8.229 |

### All held-out tasks

| Comparison | n | Rate A | Rate B | Diff (B − A) [95% CI] | W/L/T | Sign p | Majority only B / only A | McNemar p | Not run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frozen vs baseline | 40 | 0.85 | 0.975 | 0.125 [0.04167, 0.225] | 8/0/32 | 0.007812 | 4 / 0 | 0.125 | 0 |

### Cells that did not pass

| Task | Configuration | Attempt | Verdict | Reason |
| --- | --- | --- | --- | --- |
| amc12_2000_p1 | baseline | completed | fail | Lean error at line 18, column 65: maximum recursion depth has been reached use `set_option maxRecDepth <num>` to increase limit use `set_option diagnostics true |
| amc12_2000_p1 | baseline | completed | fail | Lean error at line 21, column 65: maximum recursion depth has been reached use `set_option maxRecDepth <num>` to increase limit use `set_option diagnostics true |
| aime_1991_p9 | baseline | completed | fail | Lean error at line 33, column 31: mod_cast has type   ↑m = 29 / 15 but is expected to have type   m = 29 / 15 |
| amc12a_2013_p4 | baseline | completed | fail | Lean error at line 8, column 55: unsolved goals ⊢ (2 ^ 2014 + 2 ^ 2012) / (2 ^ 2014 - 2 ^ 2012) = 5 / 3 |
| amc12a_2013_p4 | baseline | completed | fail | Lean error at line 8, column 55: unsolved goals ⊢ (2 ^ 2014 + 2 ^ 2012) / (2 ^ 2014 - 2 ^ 2012) = 5 / 3 |
| imosl_2007_algebra_p6 | F | completed | fail | no proof produced |
| amc12_2000_p1 | baseline | completed | fail | Lean error at line 16, column 72: maximum recursion depth has been reached use `set_option maxRecDepth <num>` to increase limit use `set_option diagnostics true |
| imosl_2007_algebra_p6 | baseline | completed | fail | no proof produced |
| algebra_abpbcpcageq3_sumaonsqrtapbgeq3onsqrt2 | baseline | completed | fail | Lean error at line 47, column 53: Tactic `rewrite` failed: Did not find an occurrence of the pattern   ?m.1413 / √2 ≤ ?m.1415 / (2 * √2 * u ^ 3) in the target e |
| imo_1997_p5 | baseline | completed | fail | no proof produced |
| imosl_2007_algebra_p6 | F | completed | fail | no proof produced |
| amc12b_2021_p18 | baseline | completed | fail | Lean error at line 24, column 4: No goals to be solved |
| aime_1991_p9 | baseline | completed | fail | Lean error at line 33, column 29: mod_cast has type   ↑m = 29 / 15 but is expected to have type   m = 29 / 15 |
| amc12a_2013_p4 | baseline | completed | fail | Lean error at line 9, column 38: unsolved goals ⊢ 2 ^ 2014 = 4 * 2 ^ 2012 |
| imosl_2007_algebra_p6 | baseline | completed | fail | no proof produced |
| amc12_2000_p6 | baseline | completed | fail | Lean error at line 16, column 61: No goals to be solved |
| aime_1991_p9 | baseline | completed | fail | Lean error at line 38, column 30: mod_cast has type   ↑m = 29 / 15 but is expected to have type   m = 29 / 15 |
| amc12_2000_p6 | baseline | completed | fail | Lean error at line 16, column 44: No goals to be solved |
| mathd_numbertheory_135 | baseline | completed | fail | Lean error at line 18, column 65: unsolved goals A B C : ℕ h₂ : List.Pairwise (fun x1 x2 => x1 ≠ x2) [A, B, C] h₃ : {A, B, C} ⊂ Finset.Icc 0 9 h₄ : Odd A ∧ Odd  |
| imosl_2007_algebra_p6 | baseline | completed | fail | no proof produced |
| imosl_2007_algebra_p6 | F | completed | fail | no proof produced |

## Audit

- smoke: 6 records, 0 with a harness error, 0 interrupted cell(s), charged $0.3506
- dev: 330 records, 0 with a harness error, 0 interrupted cell(s), charged $13.93
- held_out: 240 records, 0 with a harness error, 0 interrupted cell(s), charged $19.73

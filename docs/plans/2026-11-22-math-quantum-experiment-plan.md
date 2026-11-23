# Experiment plan: Lean 4 on miniF2F and Qiskit on QASMBench

- **Status:** proposed 2026-11-22, waiting for approval. No model has been called, no search has
  run, and none of the 70 selected math problems has been touched by any tool.
- **Deadline:** 27 November 2026 (Europe/Istanbul).
- **Engine:** the study protocol of [ADR 2026-11-22](../adr/2026-11-22-domain-adapter.md).
- **Frozen splits (committed with this plan):**
  [`studies/2026-11-math-minif2f/split.json`](../../studies/2026-11-math-minif2f/split.json) and
  [`studies/2026-11-quantum-qasmbench/split.json`](../../studies/2026-11-quantum-qasmbench/split.json),
  each produced by the `select_tasks.py` next to it (`--check` reproduces the file byte for byte).

These are subset studies. Their numbers are not leaderboard scores and must never be presented as
such.

## Shared protocol

1. **Fixed splits.** Dev and held-out IDs are recorded above, chosen by a recorded seed, before any
   search. The search code receives dev IDs only.
2. **Search** on dev tasks only, inside the budget below.
3. **Freeze.** The chosen configuration, its SHA-256, the baseline and comparators, the dev
   results and a UTC timestamp go into a write-once `freeze.json`, committed to git before any
   held-out task runs.
4. **Held-out, once.** Baseline, frozen configuration and named comparators run on the held-out
   tasks in a seeded interleaved order. A cell that has a record is never run again.
5. **Independent verifier.** Only verified results count; `unverifiable` is not a pass.
6. **Report** per domain in `docs/results/<date>-<domain>.md`, with the raw JSON records and a
   reproduce script, and with every field the session request lists. "No improvement found" is a
   valid result.

### Disclosure: what was run before this plan

To size verification limits and runtimes, a feasibility probe (scratch code, not committed)
compiled **every pool-A circuit and up to 13 pool-B circuits** (sat_n11, multiply_n13, bv_n14,
qf21_n15, multiplier_n15, dnn_n16, qec9xz_n17, bigadder_n18, qft_n18, bv_n19, qram_n20,
ghz_state_n23, wstate_n27) with the two fixed comparators (the default pass manager and
`optimization_level=3`, seed 1234) and checked equivalence, **before the split existed**. Those
numbers set the limits below and are not part of any comparison; the held-out evaluation
recomputes everything. The math probe compiled one hand-written proof of
`mathd_algebra_10` (valid split, not selected for dev) and two deliberately bad files; no held-out
math problem has been opened by any tool.

---

## A. Mathematics: Lean 4 on a miniF2F subset

### A1. Dataset and pins

| Item | Pin |
| --- | --- |
| Benchmark | `yangky11/miniF2F-lean4` at `5746b7d6c47855ce1294bed87329618ff7f1bc31` (2026-03-25), 244 valid and 244 test problems, one file each |
| License | MIT, Copyright (c) Meta Platforms, Inc. and affiliates |
| Lean | `leanprover/lean4:v4.24.0` (commit `797c613eb9b6`), via elan 4.4.0 |
| Mathlib | `v4.24.0` = `f897ebcf72cd16f89ab4577d0c826cd14afaafc7`, and its manifest: batteries `8da40b7`, aesop `725ac8c`, Qq `dea6a33`, ProofWidgets `v0.0.74`, plausible `dfd06eb`, importGraph `d768126`, LeanSearchClient `99657ad`, Cli `91c18fa` (identical to the benchmark's `lake-manifest.json`) |
| Kernel replay (optional) | `leanprover/lean4checker` tag `v4.24.0` = `da26eb05e995` |
| Anthropic SDK | `anthropic==2.12.0` (Python) |
| Model | `claude-opus-5-5`, one model for the whole experiment |

Why this port: it is the Lean 4 port that keeps receiving statement fixes (7 test statements
corrected in February–March 2026, PR #33), and the Lean and Mathlib versions are pinned in the
repository. Its README says it is "NOT maintained regularly"; the alternatives are worse fits:
`roozbeh-yz/miniF2F_v2` (Apache-2.0, corrected statements) ships JSONL without a pinned Lean
project, and `openai/miniF2F` and `facebookresearch/miniF2F` are Lean 3.

Measured here: elan, Lean and the Mathlib binary cache (7,335 files, 6.2 GB) install in 81 s; a
proof check including `import Mathlib` takes 4.5 s on 4 cores.

### A2. Split

- Seed `20261122`. Every problem is ranked by `sha256(f"{seed}:{split}:{id}")`; the first 30 of
  the valid split are dev, the first 40 of the test split are held-out.
- **Dev (30, valid):** aimeII_2020_p6, mathd_numbertheory_30, imo_1964_p1_2, mathd_algebra_405,
  amc12b_2002_p3, induction_ineq_nsqlefactn, induction_sum_1oktkp1, mathd_algebra_73,
  algebra_sqineq_2at2pclta2c2p41pc, aime_1987_p8, mathd_algebra_493, mathd_numbertheory_92,
  aime_1996_p5, mathd_numbertheory_284, amc12a_2019_p21, mathd_algebra_327,
  mathd_numbertheory_32, mathd_algebra_126, imo_1961_p1, mathd_algebra_109, mathd_algebra_185,
  mathd_numbertheory_43, mathd_numbertheory_252, amc12a_2008_p15, mathd_numbertheory_461,
  mathd_algebra_31, algebra_manipexpr_apbeq2cceqiacpbceqm2, mathd_algebra_455, imo_2006_p3,
  mathd_numbertheory_709.
- **Held-out (40, test):** mathd_numbertheory_222, mathd_algebra_598, aime_1991_p9,
  mathd_algebra_176, imo_1962_p2, mathd_algebra_484, mathd_numbertheory_150, mathd_algebra_359,
  mathd_algebra_320, mathd_numbertheory_233, algebra_absapbon1pabsapbleqsumabsaon1pabsa,
  algebra_sqineq_unitcircatbpabsamblt1, mathd_numbertheory_135, imo_1997_p5, amc12b_2021_p18,
  amc12a_2013_p4, mathd_numbertheory_618, mathd_algebra_137, numbertheory_2pownm1prime_nprime,
  algebra_abpbcpcageq3_sumaonsqrtapbgeq3onsqrt2, amc12_2000_p6, mathd_numbertheory_551,
  mathd_algebra_142, amc12_2000_p1, mathd_numbertheory_341, amc12a_2021_p18, amc12a_2020_p15,
  mathd_numbertheory_582, imosl_2007_algebra_p6, mathd_numbertheory_320, mathd_algebra_289,
  mathd_algebra_332, amc12b_2021_p3, mathd_algebra_440, mathd_algebra_188, mathd_algebra_170,
  mathd_algebra_293, mathd_numbertheory_12, mathd_numbertheory_314, mathd_numbertheory_328.
- **Leakage checks.** Recorded in `split.json`: no shared IDs, and no statement (name removed,
  whitespace collapsed) shared between any dev and held-out problem; the whole dataset has no
  valid/test duplicate statements. At run time the adapter also checks that no held-out theorem
  name or statement occurs in the prompts or the retrieval index (below). Prompts contain no
  worked examples from miniF2F at all.

### A3. System under test

An LLM prover loop inside the `lean` adapter, using the official Anthropic Python SDK:

- `model="claude-opus-5-5"`; the served model (`response.model`) is recorded and asserted on every
  call; a mismatch invalidates the attempt.
- `output_config={"effort": "medium"}`, set explicitly on every request. It is also this model's
  default, but the run must not depend on a default that could change.
- Thinking: adaptive, which cannot be turned off on this model. No `temperature`, `top_p` or
  `top_k` (rejected on this model), so sampling is not seedable; variability is measured with
  3 repeats per task.
- **No refusal fallbacks**: no `fallbacks` parameter, no fallback beta, no fallback middleware. A
  response with `stop_reason == "refusal"` is a failed round; refusals are counted and reported
  with their category.
- `max_tokens=32000` per call (bounds the worst case at about $0.64 of output per call); a
  `max_tokens` stop is a failed round and is counted.
- Streaming with `get_final_message()`; automatic prompt caching on. SDK retries (`max_retries=2`)
  cover only transport errors, 429 and 5xx on the same model.
- The API key is read from the environment and never printed, logged or committed. **No key is
  available in this environment yet**: before the smoke test one has to be provided, either as an
  environment secret for `api.anthropic.com` (injected by the network proxy) or as
  `ANTHROPIC_API_KEY`.
- The model returns the tactic proof only. The harness splices it after the original
  `:= by` of the untouched problem file, so the statement cannot change; the verifier checks that
  anyway (A7).

### A4. Search space

| Knob | Levels | What changes in behaviour |
| --- | --- | --- |
| `planning` | off, on | On: a first call writes an informal proof plan (no Lean); the plan is part of every later prompt of that attempt. |
| `retrieval` | none, mathlib_names | Names: the model gets a `search_mathlib(query)` tool returning up to 20 declarations (name and type) from an index of the pinned Mathlib, built once from the environment of `import Mathlib` (the `Archive/` folder, which holds some IMO solutions, is not part of it). |
| `context_limit` | 12k, 48k tokens | Cap on the prompt the harness builds for a round: instructions and the problem always stay; plan, earlier attempts, Lean errors and tool results are cut oldest first. Each round starts a fresh request, so no conversation history is ever edited. |
| `tactic_tools` | none, probe | Probe: the model gets a `try_tactic(goal, tactics)` tool that elaborates `example <goal> := by <tactics>` against the pinned Mathlib (60 s) and returns success or Lean's messages; search tactics (`exact?`, `apply?`, `hint`) are allowed inside it. |
| `feedback_rounds` | 0, 3 | After a failed proof, the harness compiles it in-loop and returns the first Lean errors for another round, at most this many times. |
| `retries` | 1, 2 | Independent fresh attempts (new conversation) if the previous one did not produce a checked proof. |

Per-attempt caps for every configuration: at most 4 tool calls per round, 12 API calls per attempt
and 20 minutes of wall-clock time. My reading of "allowed tactic tools" and "context limit" is the
one above; if the brief means something else, these two rows change before any paid run.

### A5. Baseline

`planning=off, retrieval=none, context_limit=12k, tactic_tools=none, feedback_rounds=0,
retries=1`: one call, one proof, verified.

### A6. Objective on dev

1. Feasibility: mean cost per attempt at most 4 times the baseline's (measured on dev), so a
   configuration cannot win by spending without limit.
2. Maximise the mean per-problem verified rate (repeats averaged within a problem first).
3. Tie (within 1/30): lower cost per verified proof.

### A7. Verifier: the Lean kernel

For each final proof, in a fresh process with the pinned toolchain:

1. Build the candidate file: the original problem file with its final `sorry` replaced by the
   proof. Check that everything up to `:= by` is byte-identical to the original file.
2. Reject before compiling if the proof contains `sorry`, `admit`, `axiom`, or metaprogramming that
   could add unchecked declarations (`run_tac`, `run_cmd`, `elab`, `macro`, `syntax`, `#eval`,
   `#exit`, `unsafe`, `implemented_by`, `extern`, `debug.skipKernelTC`, `addDecl`, `modifyEnv`), or
   any line at column 0 (which would start a new top-level command).
3. `lake env lean --json` with a 300 s wall-clock timeout (the files set `maxHeartbeats 0`). Pass
   requires exit code 0, no error message and no `hasSorry` message.
4. `#print axioms <theorem>` is appended by the verifier; its message is accepted only at the
   verifier's own line (JSON positions cannot be forged from inside the proof). Allowed axioms:
   `propext`, `Classical.choice`, `Quot.sound`. Anything else (`sorryAx`, `Lean.ofReduceBool`
   from `native_decide`, a new axiom) fails.
5. Optional defence in depth: replay the compiled module with `lean4checker v4.24.0`.

Checked by hand on the probe: a correct proof passes with exactly the three axioms; a `sorry`
proof is caught twice (`hasSorry` and `sorryAx`); an undefined-lemma proof fails to compile.

**What it does not check:** that the formal statement matches the informal competition problem
(miniF2F_v2 documents many v1 statements that do not, some easier or harder than the original);
soundness bugs in Lean or Mathlib (the kernel is trusted); how a proof was found.

### A8. Metrics

Verified (per attempt and per problem), cost per attempt and per verified proof (from `usage` and
the pinned prices: $4 per million input tokens, $20 per million output tokens, cache reads and
writes at their listed rates; to be re-checked against the live price page before the first paid
call), wall-clock time (model and Lean separately), API calls, feedback rounds and retries used,
tool calls by tool, refusals with category, `max_tokens` stops, API errors.

### A9. Search procedure and budget

| Phase | What runs | Attempts | Est. API calls |
| --- | --- | --- | --- |
| Smoke | 3 dev problems × {baseline, all knobs on} × 1 repeat | 6 | ~25 |
| Stop | Show measured cost per attempt and the projected total; continue only with approval | | |
| Stage 1 | Baseline B, F = feedback 3, then F plus one knob each: retrieval, tactic probe, planning, context 48k, retries 2 (7 configurations) × 30 dev × 1 repeat | 210 | ~540 |
| Stage 2 (conditional) | B and the Stage 1 winner, 2 more repeats each | 120 | ~240 |
| Freeze | Winner by A6 (on 3 repeats if Stage 2 ran, else on 1) | | |
| Held-out | {B, frozen} × 40 × 3 repeats | 240 | ~480 |

If two or more single-knob additions beat F by at least 2 problems in Stage 1, their combination
is scored as an eighth Stage 1 configuration, provided the held-out reserve stays covered.

| Cost per call | Smoke | Stage 1 | Stage 2 | Held-out | Total |
| --- | --- | --- | --- | --- | --- |
| $0.08 | $2 | $43 | $19 | $38 | $102 |
| $0.13 (planning value) | $3 | $70 | $31 | $62 | $166 |
| $0.20 | $5 | $108 | $48 | $96 | $257 |

Budget rules, enforced by the core's budget gate (cost reserved before a cell starts, so in-flight
cells cannot overshoot):

- Hard cap **$150** for everything, smoke test included. The core stops before exceeding it and
  asks.
- The held-out phase is reserved first: its projected cost (from dev costs of B and the frozen
  configuration, plus 25%) is never spent on search.
- Stage 2 runs only if the remaining budget after Stage 1 covers both Stage 2 and the held-out
  reserve.
- If the smoke test projects more than $150 even without Stage 2, the Stage 1 configurations are
  dropped in this order: context 48k, retries 2, planning. Context comes first because it changes
  behaviour only when a built prompt exceeds 12k tokens, which the smoke test measures. Lowering
  effort to `low` is a separate decision for you, with a new smoke test.
- At the planning value ($0.13 per call) this means: no Stage 2, selection on 1 repeat, and the
  context-48k configuration dropped (projected $144 with the held-out reserve; expected spend
  about $128). Every knob that is not searched is named in the report, with the reason.

Runtime: about 30–90 s per API call at medium effort (to be measured in the smoke test) and 5–60 s
per Lean check; 4 attempts in parallel. Stage 1 about 2–3 hours, held-out about 2 hours.

### A10. Held-out comparison

- Primary: per-problem verified rate over 3 repeats, frozen minus baseline. Mean difference with
  a 95% percentile bootstrap interval over problems (10,000 resamples, seed `20261122`) and an
  exact two-sided sign test on problems that differ.
- Secondary: exact McNemar test on the per-problem majority outcome (verified in at least 2 of 3
  repeats); pass@1 and pass@3; cost per verified proof; latency; failures by type; refusal count.
- Variability: number of problems with mixed outcomes across repeats, per configuration.

### A11. Seeds

Split `20261122`; run order (interleaving problems, configurations and repeats) `7`; bootstrap
`20261122`. Model sampling is not seedable (stated in the report).

### A12. Risks and limitations

- **Contamination.** miniF2F-test problems and public Lean proofs of them (DeepSeek-Prover,
  Kimina, Goedel) predate the model. Absolute pass rates can be inflated; the paired comparison is
  less affected, but memorisation can interact with the knobs (a memorised proof needs no
  feedback).
- Misformalized statements (see A7) can make a problem trivially true or unprovable for both arms.
- 30 dev problems at 1 repeat can only detect large effects; the held-out test is what protects
  the claim.

---

## B. Quantum compilation: Qiskit on QASMBench

### B1. Dataset and pins

| Item | Pin |
| --- | --- |
| Benchmark | `pnnl/QASMBench` at `357b942396d5c2b7cbc1c229c585a6ef5ccaebac` (2025-01-19) |
| License | Battelle Memorial Institute BSD-style license, with the PNNL/DOE notice (`LICENSE`, `NOTICE`); circuits are read from a checkout at the pinned commit, not copied into this repository |
| Compiler | `qiskit==2.6.2` (Apache-2.0), Python 3.12 |
| Exact check | `numpy==2.5.5` |
| Decision-diagram check (option B) | `mqt.qcec==3.12.0` (MIT) |

### B2. Target (one, fixed)

`CouplingMap.from_heavy_hex(5, bidirectional=True)`: a heavy-hex lattice of 57 physical qubits and
128 directed edges (listed in `split.json`), basis gates `cz`, `rz`, `sx`, `x`, built with
`Target.from_configuration`. Distance 5 is the smallest heavy-hex that fits the 27-qubit circuits
of pool B, so the same target serves both options.

### B3. Eligibility and skipped circuits

Rules use the input circuit only (see `select_tasks.py`): it parses with Qiskit 2.6.2; after
removing final measurements it has no measurement, reset or classical control; it has at least
one multi-qubit gate. Pool A: up to 10 qubits. Pool B: 11–27 qubits.

| Outcome | Circuits |
| --- | --- |
| Eligible, pool A | 32 |
| Eligible, pool B | 19 |
| Mid-circuit measurement, reset or classical control | 9: small/bb84_n8, small/inverseqft_n4, small/ipea_n2, small/qec_sm_n5, small/shor_n5, medium/bwt_n21, medium/cc_n12, medium/seca_n11, medium/square_root_n18 |
| Does not parse with Qiskit 2.6.2 (gate bodies use the global register) | 5: small/hhl_n10, small/vqe_uccsd_n4, small/vqe_uccsd_n6, small/vqe_uccsd_n8, medium/hhl_n14 |
| No multi-qubit gate | 1: small/qrng_n4 |
| No `<name>.qasm` in the folder | 15: medium/gcm_n13 and 14 folders in `large/` (listed in `split.json`) |
| `large/` (28+ qubits) | 56: beyond any check used here |

### B4. Split

Seed `20261122`. Per pool, circuits are ranked by `sha256(f"{seed}:{id}")` and alternate
held-out, dev, held-out, ..., so adding pool B never moves a pool-A circuit.

- **Pool A dev (16):** iswap_n2, vqe_n4, wstate_n3, ising_n10, bell_n4, teleportation_n3,
  cat_state_n4, fredkin_n3, toffoli_n3, sat_n7, qec_en_n5, quantumwalks_n2, adder_n10,
  error_correctiond3_n5, qpe_n9, qft_n4.
- **Pool A held-out (16):** linearsolver_n3, dnn_n8, dnn_n2, pea_n5, lpn_n5, qaoa_n6, hhl_n7,
  variational_n4, simon_n6, basis_trotter_n4, qaoa_n3, basis_change_n3, adder_n4, deutsch_n2,
  hs4_n4, grover_n2.
- **Pool B dev (9):** bigadder_n18, vqe_n24, qft_n18, ising_n26, knn_n25, qec9xz_n17,
  multiplier_n15, bv_n19, sat_n11.
- **Pool B held-out (10):** factor247_n15, dnn_n16, ghz_state_n23, cat_state_n22, multiply_n13,
  bv_n14, wstate_n27, qram_n20, swap_test_n25, qf21_n15.

The split is by circuit, so some families appear on both sides (adder_n10 dev, adder_n4
held-out). A compiler configuration does not learn circuit content, but the report says so.

### B5. Search space

Stage 1, full grid (960 configurations), other arguments at their defaults:

| Knob | Levels |
| --- | --- |
| `optimization_level` | 0, 1, 2, 3 |
| `layout_method` | default, dense, sabre, trivial |
| `routing_method` | default, basic, lookahead, sabre (`none` cannot route) |
| `translation_method` | default, synthesis, translator |
| `seed_transpiler` | 0, 1, 2, 3, 4 |

Stage 2: the 10 best feasible Stage 1 configurations × synthesis and approximation choices
(110 new configurations):

| Knob | Levels |
| --- | --- |
| `approximation_degree` | 1.0, 0.99 (approximate outputs pass only if they still verify) |
| `qubits_initially_zero` | True, False (whether synthesis may treat untouched qubits as clean ancillas) |
| `hls_config` for `mcx` | default, noaux_v24, gray_code |

Not searched: `unitary_synthesis_method` other than `default` (`aqc` is approximate, `sk` and
`gridsynth` target Clifford+T, `clifford` covers only Clifford unitaries), and scheduling (no
timing in the objective).

### B6. Baseline and comparator, exactly

- **Baseline:** `generate_preset_pass_manager(target=TARGET, seed_transpiler=0)` with every other
  argument at its Qiskit 2.6.2 default: `optimization_level=2`, `layout_method`, `routing_method`,
  `translation_method`, `init_method`, `optimization_method`, `scheduling_method` all `None`
  (default plugins), `approximation_degree=1.0`, `unitary_synthesis_method="default"`,
  `hls_config=None`, `qubits_initially_zero=True`. The one deviation from defaults is the seed:
  the default `None` is not reproducible.
- **Comparator:** the same with `optimization_level=3`, reported on dev and held-out, never
  selected.

### B7. Objective on dev

0. Before the search, the baseline is compiled and checked on every dev circuit. A dev circuit
   whose baseline output cannot be verified is dropped from the objective and listed (decided on
   dev data, before any candidate runs).
1. Feasibility: every remaining dev circuit's output verifies.
2. Minimise the geometric mean over those dev circuits of
   `(two_qubit_gates + 1) / (baseline_two_qubit_gates + 1)`.
3. Tie (within 1e-9): the same ratio for depth; then median compile time; then config hash.

### B8. Verifier

The input is the circuit after `remove_final_measurements`; the output is the compiled circuit
with its layout. Equivalence means: for every input state of the circuit's qubits, with every
other physical qubit starting in |0⟩, the compiled circuit produces the original's output (up to
one global phase) on the physical qubits given by the final layout, with the other qubits back
in |0⟩.

- **Exact check (pool A).** Simulates the compiled circuit on its active physical qubits only (any
  qubit with a gate, plus the initial and final positions of every circuit qubit; idle ancillas
  stay |0⟩) for all 2^n basis inputs, with the four basis-gate matrices written out in the
  adapter, not taken from the transpiler. The reference unitary comes from
  `qiskit.quantum_info.Operator` of the input circuit. Pass: `|⟨E, Ψ⟩| / 2^n ≥ 1 − 1e-9` and, after
  removing the phase, every amplitude within 1e-7. More than 14 active qubits: `unverifiable`.
  In the probe (which used Qiskit's `Operator` on the active qubits instead) the default outputs
  used at most 11 active qubits and the slowest check took 23 s.
- **Decision-diagram check (pool B, option B).** `mqt.qcec.verify` with the alternating and ZX
  checkers, no simulation checker, 60 s timeout. Only `equivalent` or
  `equivalent_up_to_global_phase` counts; `no_information`, `probably_equivalent` or a timeout is
  `unverifiable`. It also runs on every pool-A output; any disagreement with the exact check stops
  the study until it is explained.
- In the probe on this target, QCEC proved the default and level-3 outputs of 7 circuits
  equivalent (adder_n4, qpe_n9, adder_n10, sat_n11, multiply_n13, bv_n14, qf21_n15; at most 0.8 s
  each) and reported `not_equivalent` for every deliberately broken copy (one two-qubit gate
  dropped; at most 8 s). On dnn_n16 it ran for more than 10 minutes despite its 60 s timeout, so
  the adapter runs QCEC in a child process that is killed at 90 s (`unverifiable`), and some
  pool-B circuits may not be checkable at all. The adapter's tests add more broken cases (wrong
  final permutation, a phase error on one qubit, swapped qubits) for both checkers.

Where checking stops: pool A up to 10 circuit qubits and 14 active qubits (exact); pool B up to 27
qubits within the 60 s timeout, killed at 90 s (decision diagrams); nothing in `large/`. Only verified outputs count in any
claim; a configuration whose output is `unverifiable` on a circuit fails that circuit.

**What it does not check:** circuits with measurement-dependent behaviour (excluded), how Qiskit's
QASM parser and gate definitions read the source (the reference unitary uses them too), hardware
noise, calibration and timing, and whether a circuit implements the algorithm its name suggests.

### B9. Metrics

Two-qubit gate count, depth, size, compile time (median of 3 runs, output unchanged across them),
active qubits, verdict and checker, feasibility.

### B10. Search budget, cost and runtime

$0 in API cost. At most 1,100 configurations and 8 CPU-hours including verification.
Configurations are evaluated in a seeded random order (seed `20261122`), so if the CPU budget runs
out the evaluated ones are a random subsample of the grid; the rest are listed as not evaluated.
Measured: compiling all 33 pool-A circuits at the default settings takes 0.37 s; outputs are
identical for the same seed; exact checks take milliseconds to 23 s, QCEC checks at most 8 s on
the circuits it finished and up to the 90 s kill on the others. With checks cached by output
hash, the search should take about 1–3 hours on 4 cores; the held-out phase minutes.

### B11. Held-out comparison

- Baseline, frozen configuration and the level-3 comparator, once each, on the held-out circuits.
- A held-out circuit whose baseline output cannot be verified is left out of the comparison and
  listed. Where the baseline verifies but the frozen configuration's output does not, the circuit
  counts as a loss in the sign test and is listed; having no verified gate count, it is left out
  of the mean difference, whose n is reported.
- Primary: per-circuit difference in two-qubit gates (frozen minus baseline) on circuits where
  both verified: mean with a 95% paired bootstrap interval (10,000 resamples, seed `20261122`),
  median, wins / losses / ties and an exact two-sided sign test; the geometric-mean ratio with a
  bootstrap interval.
- Secondary: depth (same analysis), compile time, level 3 against the baseline.
- Robustness, pre-registered: baseline and frozen configuration with 5 seeds not used in the
  search (`1001`–`1005`) replacing the seed knob, compared per circuit on the seed average. This
  separates a better configuration from a lucky seed.
- Reported for pool A, pool B and both.

### B12. Seeds

Split `20261122`; transpiler seeds `0`–`4` (search), `0` (baseline and comparator),
`1001`–`1005` (robustness); bootstrap `20261122`.

### B13. Risks and limitations

- Small effects are likely: in the probe the default and level 3 differed on only 3 of the 33
  small circuits, and level 3 was worse on one of them (qpe_n9: 81 against 79 two-qubit gates).
  "No improvement" is a likely and valid outcome.
- 16 held-out circuits (option A) give little statistical power; option B raises it to 26.
- Choosing a transpiler seed on dev mostly selects luck; the robustness analysis exposes that.


### B14. Amendments before the search (2026-11-22, before any search cell ran)

Each amendment was decided on dev circuits only, before the first search cell. Each was checked
with the adapter and again with `studies/2026-11-quantum-qasmbench/amendment_checks.py`.

1. **Lookahead routing is not searched.** Stage 1 varies `routing_method` over default, basic and
   sabre: 4 × 4 × 3 × 3 × 5 = **720 configurations**, not 960.
   - **Evidence.** Lookahead did not finish within 30 s on bv_n19, qec9xz_n17 or sat_n7 (level 2,
     default layout). The adapter's probe found the same on 9 of 9 pool-B dev circuits with dense
     layout at level 1, and on 8 of 9 with the default layout. It also timed out on sat_n7 and on
     qpe_n9 or adder_n10 in pool A, depending on the layout. On sat_n11 it ran for more than 20
     minutes. Basic routing compiled each of these circuits in under 0.3 s.
   - **Why it is dropped.** 240 lookahead configurations × 22 circuits would exceed the 8 CPU-hour
     budget of B10 many times over. With a time limit they would be infeasible on most circuits
     anyway.
2. **`hls_config` for `mcx` is not searched in Stage 2.** No dev circuit contains an MCX gate.
   Seven contain Toffoli gates, which this setting does not affect. Compiling every dev circuit
   except vqe_n24 at the baseline with `noaux_v24` and with `gray_code` gave outputs identical to
   the default. Searching it would add 2 × 10 configurations whose outputs are identical. The
   tie-break would then pick its level by compile-time noise. Stage 2 is now 10 × 2 × 2 = 40
   configurations, of which **30 are new**.
3. **Compile time limit.** All repetitions of one attempt together get 1,200 s. Past that, the
   worker is killed and the attempt is a `timeout`, which fails its circuit like an unverified
   output. The plan set no limit. The slowest baseline, medium/vqe_n24, needs about 440–490 s for
   its 3 compilations.
4. **Verifier details** (adapter `qiskit-qasmbench` 1; B8 otherwise unchanged):
   - **ZX only without ancillas.** The decision-diagram check uses the ZX checker only when no
     ancilla qubit is active. Otherwise it uses the alternating checker alone. The reason: mqt.qcec
     3.12.0's ZX checker accepted all 53 outputs of adder_n4 with an X gate deliberately left on an
     idle ancilla, while the alternating checker rejects them. The case is in the adapter's tests.
   - **Memory limit.** A QCEC job may use at most its current size plus min(half of RAM, 3/4 of
     available memory). Running out is `unverifiable`. On medium/vqe_n24 an unlimited job was
     killed by the kernel twice.
   - **Canonical QPY.** The compiled circuit is stored as QPY version 16, with ancilla labels in
     physical order, because plain QPY bytes vary between identical compilations.
   - **Baseline call.** It passes `optimization_level=2`, `approximation_degree=1.0` and
     `qubits_initially_zero=True` explicitly, besides the seed. On all 24 dev circuits checked
     (vqe_n24 skipped for time), its outputs were identical to `generate_preset_pass_manager(
     target=TARGET, seed_transpiler=0)`.
5. **Budget.** The search space is 2 + 720 + 30 = **752 configurations** (B10: at most 1,100). The
   CPU-time cap of 8 hours stays.
6. **Report groups.** B11's per-pool results come from task tags. The adapter tags each circuit
   with its pool, and the study spec sets `stats.tag_groups: [A, B]`.

### B15. Amendments after the red team (2026-11-23, before any search cell ran)

An adversarial review attacked the verifier with the real checkers and hand-built payloads. It
used dev circuits and synthetic circuits only; no held-out circuit was opened. It found payloads
that get a false pass on pool B, and verdicts that crash or change from run to run. The study's
own compiler produces none of these payloads. The fixes make the verifier sound against them
anyway. They come with adapter `qiskit-qasmbench` 2, `target-validity/2` and `exact-isometry/2`.
No search cell had run when they were made.

1. **Strict target validity.** Each rule below closes a hole the red team reproduced:
   - **Basis gates.** A basis gate must be that standard gate: the base class matches, and a CZ is
     controlled on |1⟩.
   - **rz angles.** An rz angle must be a finite real number: an int, a float, or a parameter
     expression with no free parameter.
     - *Hole:* QCEC's importer re-parses any angle that is not a float, and drops the sign of a
       constant term. With rz(−3) stored as an int, or rz(p·0 − 0.3), a wrong output passed on
       pool B and raised a disagreement on pool A.
   - **Barriers and delays.** An instruction counts as a barrier or delay only if it really is
     one. A gate merely *named* `barrier` is outside the basis.
     - *Hole:* such a gate with an X inside passed on both pools.
   - **Global phase.** The global phase must be a finite real number.
     - *Hole:* a NaN or unbound phase raised on pool A and passed on pool B.
   - **Qubit registers.** The qubits must form exactly one register, in the circuit's own order.
     - *Hole:* QCEC numbers qubits by register; Qiskit and the exact check number them by
       `circuit.qubits`. A register listing the qubits in reverse, with every gate moved by a
       reflection of the heavy-hex lattice, passed on pool B (qec9xz_n17, bv_n19).
2. **One canonical circuit for every checker.** The exact check and QCEC both read a rebuilt
   copy of the output. It keeps the same qubits, registers and layout. Every gate is a fresh
   standard gate with a float angle. Barriers and delays, which are identities, are dropped. The
   global phase is a float. QCEC can now check an output that contains a delay; before, it could
   not import one.
3. **QCEC runs the alternating checker alone (no ZX).**
   - **The problem.** The ZX checker ran in parallel with the alternating checker, and the first
     answer won. On one payload with an rz error of 2e-8, the race passed it in 32 of 40 runs and
     failed it in 8.
   - **The evidence for dropping ZX.** On every pool-B dev baseline output, the alternating
     checker alone gives the same verdict as the race:
     - qec9xz_n17 passes in 0.3 s instead of 0.007 s;
     - bv_n19, sat_n11, multiplier_n15, bigadder_n18 and ising_n26 are unchanged, at most 1 s;
     - qft_n18 and knn_n25 are unverifiable either way (both have active ancillas, so ZX was
       already off for them).
4. **Disagreements inside the tolerance gap are unverifiable, not a stop.**
   - **Why they happen.** The two checkers measure error differently:
     - the exact check bounds every amplitude (1e-7);
     - QCEC bounds a normalized Frobenius distance (1e-8).

     An output whose error falls between the two can therefore pass one checker and fail the
     other.
   - **The new rule.** On pool A, if the exact check's largest deviation for such a pass/fail
     disagreement is between 1e-11 and 1e-5, the verdict is `unverifiable`. The output does not
     count as verified, and the report lists it.
   - **Unchanged.** Any other disagreement still stops the study (B8).
5. **Counts come from the payload.** Two-qubit gates, depth, size and physical qubits are
   counted from the verified payload, not from the attempt's own record. An attempt whose record
   misreports its payload fails.
6. **Bounded analysis and unreadable payloads.** The analysis step (target validity, counts and
   the exact check) is killed after 1,200 s, and the verdict is `unverifiable`. A payload that QPY
   cannot read back is `unverifiable` too. Before, both raised or could hang.
7. **Ancilla register name.** The canonical layout's ancilla register now takes a name that no
   input register uses. QPY writes two registers with the same name but cannot read them back. No
   dev circuit has such a register; held-out circuits were not opened to check.
8. **Protocol fixes in the core** (ADR, hardening list): these change how stages run, not what
   is compared.
   - A stage's decision to run nothing is kept.
   - A ranking stage (here: stage 2) needs every earlier stage complete.
   - The baseline's cells run first in any stage that contains it.
   - An `unavailable` attempt is a harness error.

B8's "the alternating and ZX checkers" is replaced by item 3.

---

## Decisions needed before any code

1. The contract and migration path in the ADR.
2. The sprint brief did not reach this session. Send it, or confirm that the session request and
   `annealab/PRODUCT.md` are enough.
3. Math: effort `medium`; `max_tokens=32000`; my reading of "allowed tactic tools" and "context
   limit"; the cost-capped objective (A6); the staged search and its budget rules (A9).
4. Math: an Anthropic API key available to this environment before the smoke test (A3).
5. Quantum: option A (pool A only, 32 circuits, exact check) or option B (pools A and B, 51
   circuits, QCEC for pool B and as a cross-check). I recommend B; pool-B circuits that turn out
   not to be checkable within 90 s are listed, never claimed.
6. Quantum: split by circuit (as committed) or by circuit family.
7. These plan, ADR and future result pages are kept out of the published docs site until you
   approve them.

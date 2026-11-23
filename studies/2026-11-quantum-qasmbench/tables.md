# Study `2026-11-quantum-qasmbench`: tables from results.json

Generated 2026-11-23T11:55:36+00:00 by harnesslab 0.3.0.

## Freeze

- frozen at 2026-11-23T11:22:54+00:00, freeze_sha256 `b73586158d8ece3a4761168bc4a5877be183ae4f3486b7565220525f6363cde4`
- git head at the freeze `975466b9531459a7fcd47df2e230b349a6c09b23`, spec sha256 `f07518ba66658ba59cbb63c320c253765e71998f23eac4706e4fd43cbbb8f464`
- baseline `843db842cf08`: `{'approximation_degree': 1.0, 'hls_mcx': 'default', 'layout_method': 'default', 'optimization_level': 2, 'qubits_initially_zero': True, 'routing_method': 'default', 'seed_transpiler': 0, 'translation_method': 'default'}`
- frozen `opt3` `78f2fa9955a4`: `{'approximation_degree': 1.0, 'hls_mcx': 'default', 'layout_method': 'default', 'optimization_level': 3, 'qubits_initially_zero': True, 'routing_method': 'default', 'seed_transpiler': 0, 'translation_method': 'default'}`
- comparator `opt3` `78f2fa9955a4`
- comparator `baseline@seed_transpiler=1001` `37c25e69df9e`
- comparator `baseline@seed_transpiler=1002` `1442ed84fce3`
- comparator `baseline@seed_transpiler=1003` `9fddda58b41b`
- comparator `baseline@seed_transpiler=1004` `c7ff94b36bcc`
- comparator `baseline@seed_transpiler=1005` `5fc9aa1ec429`
- comparator `frozen@seed_transpiler=1001` `8edc3fe41615`
- comparator `frozen@seed_transpiler=1002` `033559656d3a`
- comparator `frozen@seed_transpiler=1003` `40c24bef5dea`
- comparator `frozen@seed_transpiler=1004` `0391cb9b4602`
- comparator `frozen@seed_transpiler=1005` `776b7cf59a10`

## Search (dev)

- records 16506, configurations 750, per stage {'baseline': 50, 'stage1': 15796, 'stage2': 660}
- cost $0, cell time 2.459e+04 s
- verdicts {'pass': 14416, 'unverifiable': 1809, 'fail': 281}
- considered tasks 22; left out: medium/vqe_n24 (the baseline output did not verify (unverifiable)), medium/qft_n18 (the baseline output did not verify (unverifiable)), medium/knn_n25 (the baseline output did not verify (unverifiable))
- feasible configurations 274

| Rank | Label | Feasible | Pass rate | Ratios | Medians | Note |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | opt3 | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01111 |  |
| 2 | stage1:optimization_level=3,layout_method=default,routing_method=sabre,translation_method=translator,seed_transpiler=0+approximation_degree=1.0,qubits_initially_zero=false | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.012 |  |
| 3 | stage1:optimization_level=3,layout_method=default,routing_method=default,translation_method=translator,seed_transpiler=0 | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01246 |  |
| 4 | opt3+approximation_degree=1.0,qubits_initially_zero=false | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01271 |  |
| 5 | stage1:optimization_level=3,layout_method=default,routing_method=sabre,translation_method=default,seed_transpiler=0+approximation_degree=1.0,qubits_initially_zero=false | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01417 |  |
| 6 | stage1:optimization_level=3,layout_method=default,routing_method=sabre,translation_method=translator,seed_transpiler=0 | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01501 |  |
| 7 | stage1:optimization_level=3,layout_method=default,routing_method=sabre,translation_method=default,seed_transpiler=0 | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01715 |  |
| 8 | stage1:optimization_level=3,layout_method=default,routing_method=default,translation_method=translator,seed_transpiler=0+approximation_degree=1.0,qubits_initially_zero=false | yes | – | two_qubit_gates 0.996082, depth 0.991282 | compile_time_s 0.01817 |  |
| 9 | stage1:optimization_level=3,layout_method=default,routing_method=default,translation_method=synthesis,seed_transpiler=0+approximation_degree=1.0,qubits_initially_zero=false | yes | – | two_qubit_gates 0.996281, depth 1.03183 | compile_time_s 0.01626 |  |
| 10 | stage1:optimization_level=3,layout_method=default,routing_method=sabre,translation_method=synthesis,seed_transpiler=0 | yes | – | two_qubit_gates 0.996281, depth 1.03183 | compile_time_s 0.01726 |  |

## Held-out

- complete: yes (312 of 312 planned cells recorded)
- robustness: seed_transpiler over 5 levels, compared in the rows named `robustness (...)`

| Configuration | Records | Passes | Verdicts | Attempt status | Cost $ | Wall time median s |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | 26 | 22 | {'pass': 22, 'unverifiable': 3, 'fail': 1} | {'completed': 26} | 0 | 0.06742 |
| baseline@seed_transpiler=1001 | 26 | 22 | {'pass': 22, 'unverifiable': 3, 'fail': 1} | {'completed': 26} | 0 | 0.07951 |
| baseline@seed_transpiler=1002 | 26 | 22 | {'pass': 22, 'unverifiable': 4} | {'completed': 26} | 0 | 0.07767 |
| baseline@seed_transpiler=1003 | 26 | 22 | {'pass': 22, 'unverifiable': 4} | {'completed': 26} | 0 | 0.07253 |
| baseline@seed_transpiler=1004 | 26 | 22 | {'pass': 22, 'unverifiable': 4} | {'completed': 26} | 0 | 0.06151 |
| baseline@seed_transpiler=1005 | 26 | 22 | {'pass': 22, 'unverifiable': 3, 'fail': 1} | {'completed': 26} | 0 | 0.06196 |
| frozen@seed_transpiler=1001 | 26 | 22 | {'pass': 22, 'unverifiable': 3, 'fail': 1} | {'completed': 26} | 0 | 0.1186 |
| frozen@seed_transpiler=1002 | 26 | 22 | {'pass': 22, 'unverifiable': 4} | {'completed': 26} | 0 | 0.0941 |
| frozen@seed_transpiler=1003 | 26 | 22 | {'pass': 22, 'unverifiable': 4} | {'completed': 26} | 0 | 0.1183 |
| frozen@seed_transpiler=1004 | 26 | 22 | {'pass': 22, 'fail': 1, 'unverifiable': 3} | {'completed': 26} | 0 | 0.0972 |
| frozen@seed_transpiler=1005 | 26 | 22 | {'fail': 1, 'pass': 22, 'unverifiable': 3} | {'completed': 26} | 0 | 0.09491 |
| opt3 | 26 | 22 | {'pass': 22, 'unverifiable': 3, 'fail': 1} | {'completed': 26} | 0 | 0.1087 |

### All held-out tasks

| Comparison | n | Mean A | Mean B | Mean diff (B − A) [95% CI] | Median diff | Ratio B/A [95% CI] | W/L/T | Sign p | Unverified losses | Sign p (losses counted) | Excluded | Not run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frozen vs baseline: two_qubit_gates | 22 | 59.82 | 59.77 | -0.04545 [-0.6364, 0.7273] | 0 | 0.9952 [0.9856, 1.003] | 4/2/16 | 0.6875 | 0 | 0.6875 | 4 | 0 |
| frozen vs baseline: depth | 22 | 158.4 | 159.5 | 1.182 [-1.409, 4.136] | 0 | 0.9992 [0.9895, 1.007] | 2/3/17 | 1 | 0 | 1 | 4 | 0 |
| frozen vs baseline: compile_time_s | 22 | 0.0179 | 0.02091 | 0.003008 [-0.0003326, 0.007234] | 0.0006053 | 1.136 [0.972, 1.356] | 8/14/0 | 0.2863 | 0 | 0.2863 | 4 | 0 |
| frozen vs baseline: size | 22 | 310.2 | 308.6 | -1.636 [-3.727, 0] | 0 | 0.9961 [0.9916, 0.9999] | 5/1/16 | 0.2188 | 0 | 0.2188 | 4 | 0 |
| opt3 vs baseline: two_qubit_gates | 22 | 59.82 | 59.77 | -0.04545 [-0.6364, 0.7273] | 0 | 0.9952 [0.9856, 1.003] | 4/2/16 | 0.6875 | 0 | 0.6875 | 4 | 0 |
| opt3 vs baseline: depth | 22 | 158.4 | 159.5 | 1.182 [-1.409, 4.136] | 0 | 0.9992 [0.9895, 1.007] | 2/3/17 | 1 | 0 | 1 | 4 | 0 |
| opt3 vs baseline: compile_time_s | 22 | 0.0179 | 0.02091 | 0.003008 [-0.0003326, 0.007234] | 0.0006053 | 1.136 [0.972, 1.356] | 8/14/0 | 0.2863 | 0 | 0.2863 | 4 | 0 |
| opt3 vs baseline: size | 22 | 310.2 | 308.6 | -1.636 [-3.727, 0] | 0 | 0.9961 [0.9916, 0.9999] | 5/1/16 | 0.2188 | 0 | 0.2188 | 4 | 0 |
| robustness (seed_transpiler averaged over 5 levels): two_qubit_gates | 22 | 59.97 | 59.28 | -0.6909 [-1.709, 0.05455] | 0 | 0.9952 [0.9895, 1] | 5/2/15 | 0.4531 | 0 | 0.4531 | 4 | 0 |
| robustness (seed_transpiler averaged over 5 levels): depth | 22 | 158.5 | 158.1 | -0.4 [-1.073, 0.1818] | 0 | 0.9977 [0.9926, 1.002] | 5/3/14 | 0.7266 | 0 | 0.7266 | 4 | 0 |

### Tasks tagged `A` (16)

| Comparison | n | Mean A | Mean B | Mean diff (B − A) [95% CI] | Median diff | Ratio B/A [95% CI] | W/L/T | Sign p | Unverified losses | Sign p (losses counted) | Excluded | Not run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frozen vs baseline: two_qubit_gates | 15 | 35.2 | 35.2 | 0 [-0.4, 0.4] | 0 | 0.9975 [0.9863, 1.006] | 1/1/13 | 1 | 0 | 1 | 1 | 0 |
| frozen vs baseline: depth | 15 | 112.9 | 113 | 0.1333 [-0.4, 0.8] | 0 | 1 [0.9958, 1.004] | 1/1/13 | 1 | 0 | 1 | 1 | 0 |
| frozen vs baseline: compile_time_s | 15 | 0.01453 | 0.01448 | -5.461e-05 [-0.001999, 0.001812] | 0.0004701 | 1.011 [0.8697, 1.17] | 6/9/0 | 0.6072 | 0 | 0.6072 | 1 | 0 |
| frozen vs baseline: size | 15 | 214.3 | 214.3 | 0.06667 [-0.6, 0.8] | 0 | 0.9992 [0.9956, 1.002] | 1/1/13 | 1 | 0 | 1 | 1 | 0 |
| opt3 vs baseline: two_qubit_gates | 15 | 35.2 | 35.2 | 0 [-0.4, 0.4] | 0 | 0.9975 [0.9863, 1.006] | 1/1/13 | 1 | 0 | 1 | 1 | 0 |
| opt3 vs baseline: depth | 15 | 112.9 | 113 | 0.1333 [-0.4, 0.8] | 0 | 1 [0.9958, 1.004] | 1/1/13 | 1 | 0 | 1 | 1 | 0 |
| opt3 vs baseline: compile_time_s | 15 | 0.01453 | 0.01448 | -5.461e-05 [-0.001999, 0.001812] | 0.0004701 | 1.011 [0.8697, 1.17] | 6/9/0 | 0.6072 | 0 | 0.6072 | 1 | 0 |
| opt3 vs baseline: size | 15 | 214.3 | 214.3 | 0.06667 [-0.6, 0.8] | 0 | 0.9992 [0.9956, 1.002] | 1/1/13 | 1 | 0 | 1 | 1 | 0 |
| robustness (seed_transpiler averaged over 5 levels): two_qubit_gates | 15 | 35.36 | 35.29 | -0.06667 [-0.28, 0.12] | 0 | 0.9962 [0.989, 1.002] | 2/1/12 | 1 | 0 | 1 | 1 | 0 |
| robustness (seed_transpiler averaged over 5 levels): depth | 15 | 112.6 | 112.7 | 0.02667 [-0.32, 0.4667] | 0 | 0.9997 [0.9961, 1.004] | 2/2/11 | 1 | 0 | 1 | 1 | 0 |

### Tasks tagged `B` (10)

| Comparison | n | Mean A | Mean B | Mean diff (B − A) [95% CI] | Median diff | Ratio B/A [95% CI] | W/L/T | Sign p | Unverified losses | Sign p (losses counted) | Excluded | Not run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| frozen vs baseline: two_qubit_gates | 7 | 112.6 | 112.4 | -0.1429 [-1.857, 2.143] | 0 | 0.9902 [0.9697, 1.007] | 3/1/3 | 0.625 | 0 | 0.625 | 3 | 0 |
| frozen vs baseline: depth | 7 | 255.9 | 259.3 | 3.429 [-4.286, 11.57] | 0 | 0.9973 [0.97, 1.018] | 1/2/4 | 1 | 0 | 1 | 3 | 0 |
| frozen vs baseline: compile_time_s | 7 | 0.02511 | 0.03468 | 0.00957 [0.0008221, 0.02015] | 0.004945 | 1.459 [1.05, 2.158] | 2/5/0 | 0.4531 | 0 | 0.4531 | 3 | 0 |
| frozen vs baseline: size | 7 | 515.9 | 510.6 | -5.286 [-10.43, -1] | -2 | 0.9895 [0.9787, 0.9978] | 4/0/3 | 0.125 | 0 | 0.125 | 3 | 0 |
| opt3 vs baseline: two_qubit_gates | 7 | 112.6 | 112.4 | -0.1429 [-1.857, 2.143] | 0 | 0.9902 [0.9697, 1.007] | 3/1/3 | 0.625 | 0 | 0.625 | 3 | 0 |
| opt3 vs baseline: depth | 7 | 255.9 | 259.3 | 3.429 [-4.286, 11.57] | 0 | 0.9973 [0.97, 1.018] | 1/2/4 | 1 | 0 | 1 | 3 | 0 |
| opt3 vs baseline: compile_time_s | 7 | 0.02511 | 0.03468 | 0.00957 [0.0008221, 0.02015] | 0.004945 | 1.459 [1.05, 2.158] | 2/5/0 | 0.4531 | 0 | 0.4531 | 3 | 0 |
| opt3 vs baseline: size | 7 | 515.9 | 510.6 | -5.286 [-10.43, -1] | -2 | 0.9895 [0.9787, 0.9978] | 4/0/3 | 0.125 | 0 | 0.125 | 3 | 0 |
| robustness (seed_transpiler averaged over 5 levels): two_qubit_gates | 7 | 112.7 | 110.7 | -2.029 [-4.686, 0.2286] | 0 | 0.9928 [0.9826, 1.002] | 3/1/3 | 0.625 | 0 | 0.625 | 3 | 0 |
| robustness (seed_transpiler averaged over 5 levels): depth | 7 | 256.8 | 255.5 | -1.314 [-3.086, 0.1143] | 0 | 0.9933 [0.9805, 1.001] | 3/1/3 | 0.625 | 0 | 0.625 | 3 | 0 |

### Cells that did not pass

| Task | Configuration | Attempt | Verdict | Reason |
| --- | --- | --- | --- | --- |
| small/hhl_n7 | frozen@seed_transpiler=1005 | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.53e-07) |
| small/hhl_n7 | frozen@seed_transpiler=1004 | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.53e-07) |
| medium/swap_test_n25 | baseline | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| medium/dnn_n16 | baseline@seed_transpiler=1002 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | baseline@seed_transpiler=1001 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/dnn_n16 | baseline@seed_transpiler=1005 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | baseline@seed_transpiler=1003 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| small/hhl_n7 | baseline@seed_transpiler=1001 | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.53e-07) |
| small/hhl_n7 | baseline | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.07e-07) |
| medium/dnn_n16 | frozen@seed_transpiler=1002 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | frozen@seed_transpiler=1005 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| medium/dnn_n16 | opt3 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | frozen@seed_transpiler=1002 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/dnn_n16 | frozen@seed_transpiler=1001 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | frozen@seed_transpiler=1002 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| small/hhl_n7 | opt3 | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.07e-07) |
| small/hhl_n7 | frozen@seed_transpiler=1002 | completed | unverifiable | the checkers disagree inside their tolerance gap: the exact check says pass (largest deviation 5.02e-08), QCEC says fail (QCEC: not_equivalent); not counted as  |
| medium/swap_test_n25 | frozen@seed_transpiler=1001 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | baseline | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | frozen@seed_transpiler=1005 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | baseline@seed_transpiler=1002 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| medium/dnn_n16 | frozen@seed_transpiler=1003 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| small/hhl_n7 | frozen@seed_transpiler=1001 | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.53e-07) |
| small/hhl_n7 | baseline@seed_transpiler=1002 | completed | unverifiable | the checkers disagree inside their tolerance gap: the exact check says pass (largest deviation 5.02e-08), QCEC says fail (QCEC: not_equivalent); not counted as  |
| medium/swap_test_n25 | baseline@seed_transpiler=1005 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| medium/dnn_n16 | baseline | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/dnn_n16 | baseline@seed_transpiler=1003 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | baseline@seed_transpiler=1002 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| small/hhl_n7 | baseline@seed_transpiler=1005 | completed | fail | exact check: not equivalent on 7 active qubits (fidelity 1.000000000000, largest deviation 2.53e-07) |
| medium/dnn_n16 | baseline@seed_transpiler=1001 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | opt3 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | frozen@seed_transpiler=1003 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | frozen@seed_transpiler=1004 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | opt3 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | baseline@seed_transpiler=1003 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| small/hhl_n7 | baseline@seed_transpiler=1004 | completed | unverifiable | the checkers disagree inside their tolerance gap: the exact check says pass (largest deviation 5.02e-08), QCEC says fail (QCEC: not_equivalent); not counted as  |
| medium/factor247_n15 | frozen@seed_transpiler=1001 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | frozen@seed_transpiler=1003 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| medium/dnn_n16 | frozen@seed_transpiler=1004 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| small/hhl_n7 | baseline@seed_transpiler=1003 | completed | unverifiable | the checkers disagree inside their tolerance gap: the exact check says pass (largest deviation 5.02e-08), QCEC says fail (QCEC: not_equivalent); not counted as  |
| medium/dnn_n16 | baseline@seed_transpiler=1004 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/swap_test_n25 | baseline@seed_transpiler=1004 | completed | unverifiable | QCEC: no_information (only equivalent or equivalent_up_to_global_phase count) |
| medium/factor247_n15 | frozen@seed_transpiler=1004 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | baseline@seed_transpiler=1004 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/dnn_n16 | frozen@seed_transpiler=1005 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| small/hhl_n7 | frozen@seed_transpiler=1003 | completed | unverifiable | the checkers disagree inside their tolerance gap: the exact check says pass (largest deviation 5.02e-08), QCEC says fail (QCEC: not_equivalent); not counted as  |
| medium/factor247_n15 | baseline@seed_transpiler=1005 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |
| medium/factor247_n15 | baseline@seed_transpiler=1001 | completed | unverifiable | QCEC did not finish within 90 s; its process was killed |

## Audit

- smoke: 0 records, 0 with a harness error, 0 interrupted cell(s), charged $0
- dev: 16506 records, 0 with a harness error, 0 interrupted cell(s), charged $0
- held_out: 312 records, 0 with a harness error, 0 interrupted cell(s), charged $0

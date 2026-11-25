# Related work behind REGISTRATION.md section 2

**How these sources were found.** Research agents of the session that designed this study searched
the web and arXiv on 2026-11-24 for comparisons of error feedback against resampling in formal
theorem proving. A second agent then opened each source and quoted it. The search queries were not
logged, so this is not a systematic review, and other relevant work may exist. Each entry gives
what was read, when, the passage relied on, and how the registration uses it.

## Klingner et al., arXiv 2606.05632 (v1)

- **Read:** 2026-11-24, at <https://arxiv.org/html/2606.05632v1>.
- **Passage:** "we define the difference metric Δ_k := refine@k − pass@k." Refine@k: "If an
  initial generation fails, the exact trace from the Lean verifier is extracted. The model is
  re-prompted with its previous faulty proof alongside this specific compiler feedback, iterating
  for up to k turns."
  - Table 3.1 gives Δ_32 for 14 models, Claude Opus 4.7 among them. The average is +7.7 points on
    a 50-problem miniF2F subset and −1.4 on a 50-problem miniCTX subset.
  - There is one refine run per problem, and the comparison is matched on k (calls), not on tokens
    or cost.
- **Caveat:** the paper's prose disagrees with its table on miniCTX. The text says 9 of 14 models
  are positive; the table shows 7, with 2 at zero and 5 negative.
- **Use:** the closest prior design, a call-matched comparison. It differs from this study in having
  one run per problem, 50 problems, no placebo arm and no pre-registration.

## Li & Li, arXiv 2610.22698 (v1, 15 Oct 2026; NeurIPS 2026 workshop)

- **Read:** 2026-11-24. The abstract at <https://neurips.cc/virtual/2026/171436> and the PDF were
  opened; the OpenReview page could not be.
- **Passage:** "We test frozen Qwen3.6-27B, a general-purpose model, and DeepSeek-Prover-V2-7B, a
  specialized prover model, on difficult MiniF2F problems. … Displaying the model's failed
  predecessor substantially reduces success, while on Qwen adding that attempt's compiler error
  recovers this loss. … No tested one-step feedback condition establishes an advantage over
  instruction-only resampling, nor does sequential repair with failed proofs and correct
  diagnostics at 64 attempts."
- **Use:** counter-evidence with open models on hard problems. Its "failed predecessor" and
  "compiler error" conditions correspond to our P and F. It motivates the two-sided tests.

## Olausson et al., arXiv 2306.09896 (v5, 2 Feb 2024)

- **Read:** 2026-11-24, at <https://arxiv.org/html/2306.09896>.
- **Passage:** v5 abstract: "We find that when the cost of carrying out repair is taken into
  account, performance gains are often modest, vary a lot between subsets of the data, and are
  sometimes not present at all."
  - The main comparison is matched on samples: a repair tree counts as n_p + n_p·n_fr samples.
  - A token-matched comparison (pass@t) is in Appendix A.
- **Use:** code generation, not Lean. It shows that matching the budget matters.

## Baldur, arXiv 2303.04910 (v2)

- **Read:** 2026-11-24.
- **Passage:** Table 4 reports 34.8% for "Baldur 8b generate" at 16 samples, against 36.3% for
  "generate + repair". The footnote says: "The repair approach uses half the number of samples,
  and then one repair attempt for each sample." It uses Isabelle/HOL (PISA), not Lean.
- **Use:** a gain from repair at matched samples, in another proof assistant.

## Kimina-Prover blog post (AI-MO, Hugging Face)

- **Read:** 2026-11-24, at <https://huggingface.co/blog/AI-MO/kimina-prover>.
- **Passage:** "The 16+16 attempt-and-fix strategy … achieved a success rate of 35.6%, outperforming
  the 32×1 brute-force baseline, which reached 28.8%", on "a selected subset of 59 MiniF2F-Test
  problems with the lowest win rates."
- **Use:** a self-reported gain at matched samples, with no variance reported.

## Goedel-Prover-V2, arXiv 2508.03613

- **Read:** 2026-11-24, at <https://arxiv.org/html/2508.03613>.
- **Passage:** "removing compiler feedback significantly lowers performance, confirming that specific
  error messages are crucial for effective revision". This is from a miniF2F pass@32 ablation
  ("w/o Error Messages").
- **Use:** the closest prior analogue of our P arm. It has no matched resampling arm.

## Seed-Prover, arXiv 2507.23726 (v2)

- **Read:** 2026-11-24.
- **Passage:** "we prove 99.6% problems on both MiniF2F-valid and MiniF2F-test. … Seed-Prover
  successfully proved IMO 1990 P3 and failed on IMOSL 2007 Algebra P6."
- **Use:** miniF2F is saturated for the strongest systems. Our study measures an effect at a fixed
  small budget, not the state of the art.

## Training-data cutoff of Claude Haiku 5.5

- **Read:** 2026-11-24, at <https://platform.claude.com/docs/en/about-claude/models/overview>.
- **Passage:** "| Training data cutoff | Jun 2026 | Jun 2026 | Jun 2026 | Jun 2026 |". The columns
  are Claude Fable 5.2, Opus 5.5, Sonnet 5.5 and Haiku 5.5.
- **Use:** section 3 (exposure). miniF2F has been public since 2021.

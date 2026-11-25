# Price check on the day of the first paid call

REGISTRATION.md section 8 asks for this check before the first paid call.

- **When:** 2026-11-25, 08:37 UTC, before the smoke test.
- **Page:** <https://platform.claude.com/docs/en/about-claude/pricing> (HTTP 200). SHA-256 of the
  fetched HTML: `046b1f9c7555e86052be6d4b32bc9ff1e64678d918a7f6b16aba4c6f40d6e913`.
- **Claude Haiku 5.5, per million tokens:**

  | Prompt size | Input | Output | 5-min cache write | 1-h cache write | Cache read |
  |---|---|---|---|---|---|
  | Up to 100,000 tokens | $0.10 | $0.50 | $0.125 | $0.20 | $0.01 |
  | Over 100,000 tokens | $0.50 | $2.50 | $0.625 | $1 | $0.05 |

  The page also says: "Claude Haiku 5.5 is priced by prompt length: a request whose prompt is over
  100,000 tokens pays higher prices. A request's prompt length counts all of its input tokens,
  including cache reads and cache writes."
- **Result:** these equal `src/harnesslab/domains/lean/pricing.py` (read 2026-11-24, recorded in
  `studies/2026-11-math-pilot/price_check.md`). No change is needed.
- **Not used:** the Batch API and the US-only `inference_geo` multiplier.

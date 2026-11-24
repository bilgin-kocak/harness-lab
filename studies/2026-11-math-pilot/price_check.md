# Price check before the pilot's first paid call

Plan A8 of the math study asked for a check against the live pricing page before the first paid
call, and that check was not recorded then. This file records it for the pilot.

- **When:** 2026-11-24, 05:35 UTC. The pages were fetched by a research agent of this session; the
  copies were kept in its scratchpad.
- **Pages:**
  - <https://platform.claude.com/docs/en/about-claude/pricing>, SHA-256 of the fetched HTML
    `00ea5450c7296ac70fa8eba40f402e94f4acfe22562f60dd0e843c91ba39408e`;
  - <https://claude.com/pricing> (anthropic.com/pricing redirects there), SHA-256
    `1fb203bd2a5902a5ec85c90f01e9c8fc6d8fdb2f8b5c789fd74053f648e6f14c`.

## Prices, per million tokens

| Model | Input | Output | 5-min cache write | Cache read |
|---|---|---|---|---|
| Claude Opus 5.5 | $4 | $20 | $5 | $0.20 |
| Claude Haiku 5.5, prompts ≤ 100,000 tokens | $0.10 | $0.50 | $0.125 | $0.01 |
| Claude Haiku 5.5, prompts > 100,000 tokens | $0.50 | $2.50 | $0.625 | $0.05 |

The platform page says:

> Claude Haiku 5.5 is priced by prompt length: a request whose prompt is over 100,000 tokens pays
> higher prices. A request's prompt length counts all of its input tokens, including cache reads
> and cache writes. Each request is priced on its own: a request over the threshold pays the higher
> prices even when part of its prompt is a cache hit […]

## What follows

- **Opus 5.5.** The live prices equal the table the math study used (copied on 2026-11-03), so its
  costs ($34.01 in all) stand as computed. The Opus record in `pricing.py` is unchanged, so the
  math study's frozen provenance still matches.
- **Haiku 5.5.** `pricing.py` now prices Haiku 5.5 from this page, long-prompt prices included.
  - Every prover round is a fresh request whose prompt the harness caps at `context_limit_tokens`
    (12,000 here). Baseline, F and R4 use no tools. So a pilot request should never reach 100,000
    prompt tokens.
  - The code still prices such a request correctly if one does.
- **Not used:** the Batch API (50% off). Neither is the 1.1× US-only `inference_geo` multiplier:
  the client does not set `inference_geo`, so the default global routing applies.
- **Not reconciled with the Anthropic invoice.** The costs are computed from the token counts the
  API reports.

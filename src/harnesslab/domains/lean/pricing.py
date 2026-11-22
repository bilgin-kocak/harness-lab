"""Token prices for the prover's model, and the cost arithmetic the per-attempt cap relies on.

The table was taken on 2026-11-03 from the cached model table of the Claude API skill (Claude
Opus 5.5: $4 per million input tokens, $20 per million output tokens, cache reads $0.20, 5-minute
cache writes at 1.25 times the input price). **It must be re-checked against the live pricing page
before the first paid run**; a changed price changes every cost in the study, so the check and its
date go into the study records.

Two numbers per call matter:

* :func:`usage_cost` is what a finished call cost, from the token counts the API reported.
* :func:`worst_case_cost` bounds what a call can cost before it is sent: every input token at the
  dearest input rate (a cache write, which costs more than a plain input token) plus ``max_tokens``
  output tokens. The prover sends a call only if what the attempt has spent plus this bound stays
  within the attempt's cost limit, so an attempt never spends more than its limit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

PRICES_AS_OF = "2026-11-03"
PRICES_SOURCE = "Claude API skill, cached model table of 2026-11-03"
PRICES_NOTE = "Re-check against the live pricing page before the first paid run."


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float

    @property
    def dearest_input(self) -> float:
        """The highest price an input token can be billed at (plain input or a cache write)."""
        return max(self.input, self.cache_write_5m, self.cache_read)


PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20, cache_write_5m=5.00),
}


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        known = ", ".join(sorted(PRICES))
        raise ValueError(f"no price for model {model!r} (priced: {known})") from None


def usage_cost(model: str, usage: Mapping[str, Any]) -> float:
    """Dollars for one call, from its reported usage.

    ``input_tokens`` are the uncached input tokens; cache reads and cache writes are reported
    separately by the API and priced at their own rates.
    """
    price = price_for(model)
    return (
        int(usage.get("input_tokens") or 0) * price.input
        + int(usage.get("output_tokens") or 0) * price.output
        + int(usage.get("cache_read_input_tokens") or 0) * price.cache_read
        + int(usage.get("cache_creation_input_tokens") or 0) * price.cache_write_5m
    ) / 1_000_000


def worst_case_cost(model: str, *, input_tokens: int, max_tokens: int) -> float:
    """An upper bound on one call's cost: all input at the dearest input rate, ``max_tokens`` out."""
    price = price_for(model)
    return (input_tokens * price.dearest_input + max_tokens * price.output) / 1_000_000


def price_record(model: str) -> dict[str, Any]:
    """The prices used for ``model``, with their date, for provenance."""
    return {
        "model": model,
        "usd_per_million_tokens": asdict(price_for(model)),
        "as_of": PRICES_AS_OF,
        "source": PRICES_SOURCE,
        "note": PRICES_NOTE,
    }

"""Token prices for the prover's model, and the cost arithmetic the per-attempt cap relies on.

Each priced model carries the date and source of its prices, and they go into the study records
(:func:`price_record`). **Prices must be re-checked against the live pricing page before the first
paid run of a study**; a changed price changes every cost in it.

* Claude Opus 5.5: taken on 2026-11-03 from the cached model table of the Claude API skill ($4 per
  million input tokens, $20 per million output tokens, cache reads $0.20, 5-minute cache writes at
  1.25 times the input price). The live page of 2026-11-24 shows the same prices. The record is
  kept exactly as it was, so the 2026-11 math study's frozen provenance still matches.
* Claude Haiku 5.5: taken on 2026-11-24 from the live pricing page. Haiku 5.5 is priced by prompt
  length. A request whose prompt is over 100,000 tokens (uncached input, cache reads and cache
  writes together) pays the long-prompt prices on every token of the request, output included.

Two numbers per call matter:

* :func:`usage_cost` is what a finished call cost, from the token counts the API reported.
* :func:`worst_case_cost` bounds what a call can cost before it is sent: every input token at the
  dearest input rate (a cache write, which costs more than a plain input token) plus ``max_tokens``
  output tokens, at the long-prompt prices whenever the input bound passes the model's threshold.
  The prover sends a call only if what the attempt has spent plus this bound stays within the
  attempt's cost limit, so an attempt never spends more than its limit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

# The Opus 5.5 row's provenance, unchanged since the 2026-11 math study froze it.
PRICES_AS_OF = "2026-11-03"
PRICES_SOURCE = "Claude API skill, cached model table of 2026-11-03"
PRICES_NOTE = "Re-check against the live pricing page before the first paid run."

LIVE_PRICING_PAGE = "https://platform.claude.com/docs/en/about-claude/pricing"


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


@dataclass(frozen=True)
class LongPromptPrice:
    """Prices for a request whose prompt is over ``threshold_prompt_tokens`` tokens.

    The prompt length counts every input token: uncached input, cache reads and cache writes.
    Such a request pays these prices on all of its tokens.
    """

    threshold_prompt_tokens: int
    price: ModelPrice


@dataclass(frozen=True)
class PriceSource:
    as_of: str
    source: str
    note: str


PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(input=4.00, output=20.00, cache_read=0.20, cache_write_5m=5.00),
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01, cache_write_5m=0.125),
}

LONG_PROMPT_PRICES: dict[str, LongPromptPrice] = {
    "claude-haiku-5-5": LongPromptPrice(
        threshold_prompt_tokens=100_000,
        price=ModelPrice(input=0.50, output=2.50, cache_read=0.05, cache_write_5m=0.625),
    ),
}

SOURCES: dict[str, PriceSource] = {
    "claude-opus-5-5": PriceSource(PRICES_AS_OF, PRICES_SOURCE, PRICES_NOTE),
    "claude-haiku-5-5": PriceSource(
        "2026-11-24",
        f"live pricing page {LIVE_PRICING_PAGE}, read 2026-11-24 (claude.com/pricing agrees)",
        "Prompts over 100,000 tokens (uncached input, cache reads and cache writes together) pay "
        "the long-prompt prices on every token of the request. Re-check against the live pricing "
        "page before the first paid run.",
    ),
}


def price_for(model: str) -> ModelPrice:
    """The model's standard prices (for prompts within its long-prompt threshold, if it has one)."""
    try:
        return PRICES[model]
    except KeyError:
        known = ", ".join(sorted(PRICES))
        raise ValueError(f"no price for model {model!r} (priced: {known})") from None


def price_at(model: str, prompt_tokens: int) -> ModelPrice:
    """The prices a request with ``prompt_tokens`` input tokens in all pays."""
    price = price_for(model)
    long = LONG_PROMPT_PRICES.get(model)
    if long is not None and prompt_tokens > long.threshold_prompt_tokens:
        return long.price
    return price


def usage_cost(model: str, usage: Mapping[str, Any]) -> float:
    """Dollars for one call, from its reported usage.

    ``input_tokens`` are the uncached input tokens; cache reads and cache writes are reported
    separately by the API and priced at their own rates. Together they are the prompt length that
    selects a model's long-prompt prices.
    """
    uncached = int(usage.get("input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    price = price_at(model, uncached + cache_read + cache_write)
    return (
        uncached * price.input
        + output * price.output
        + cache_read * price.cache_read
        + cache_write * price.cache_write_5m
    ) / 1_000_000


def worst_case_cost(model: str, *, input_tokens: int, max_tokens: int) -> float:
    """An upper bound on one call's cost: all input at the dearest input rate, ``max_tokens`` out.

    ``input_tokens`` bounds the whole prompt, so the call pays the long-prompt prices only if the
    bound passes the model's threshold; a prompt within the bound never pays more.
    """
    price = price_at(model, input_tokens)
    return (input_tokens * price.dearest_input + max_tokens * price.output) / 1_000_000


def price_record(model: str) -> dict[str, Any]:
    """The prices used for ``model``, with their date and source, for provenance.

    A model without long-prompt prices gets exactly the record the 2026-11 math study froze.
    """
    source = SOURCES[model]
    record: dict[str, Any] = {
        "model": model,
        "usd_per_million_tokens": asdict(price_for(model)),
        "as_of": source.as_of,
        "source": source.source,
        "note": source.note,
    }
    long = LONG_PROMPT_PRICES.get(model)
    if long is not None:
        record["long_prompt"] = {
            "threshold_prompt_tokens": long.threshold_prompt_tokens,
            "usd_per_million_tokens": asdict(long.price),
        }
    return record

"""Optional cost estimation from a user-supplied ``pricing.yaml``.

Harness Lab never hard-codes API prices.  ``reported_cost_usd`` is whatever the
harness itself reported (Claude Code reports one, Codex CLI does not).
``estimated_cost_usd`` is computed only when the user provides a pricing table
and is always stored alongside the table's version so estimates stay
attributable.

``pricing.yaml``::

    version: "2026-09-my-team"
    models:
      claude-sonnet-5:
        input_per_million: 3.0
        cached_input_per_million: 0.3
        cache_write_per_million: 3.75
        output_per_million: 15.0
      gpt-5-codex:            # prefix match: "gpt-5-codex-2026-01" also matches
        input_per_million: 1.25
        cached_input_per_million: 0.125
        output_per_million: 10.0
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from harnesslab.core.ids import hash_value
from harnesslab.core.models import UsageTotals


class ModelPricing(BaseModel):
    input_per_million: float = 0.0
    cached_input_per_million: float | None = None
    cache_write_per_million: float | None = None
    output_per_million: float = 0.0
    reasoning_output_per_million: float | None = None


class PricingTable(BaseModel):
    version: str
    currency: str = "USD"
    models: dict[str, ModelPricing] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> PricingTable:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        models = {
            name: ModelPricing(**(cfg or {})) for name, cfg in (data.get("models") or {}).items()
        }
        version = str(data.get("version") or hash_value(data, 12))
        return cls(version=version, currency=str(data.get("currency", "USD")), models=models)

    def lookup(self, model: str | None) -> ModelPricing | None:
        if not model:
            return None
        if model in self.models:
            return self.models[model]
        best: tuple[int, ModelPricing] | None = None
        for name, pricing in self.models.items():
            if model.startswith(name) and (best is None or len(name) > best[0]):
                best = (len(name), pricing)
        return best[1] if best else None

    def estimate(self, model: str | None, usage: UsageTotals) -> float | None:
        pricing = self.lookup(model)
        if pricing is None:
            return None
        per_m = 1_000_000.0
        cost = usage.input_tokens / per_m * pricing.input_per_million
        cost += usage.output_tokens / per_m * pricing.output_per_million
        cached_rate = (
            pricing.cached_input_per_million
            if pricing.cached_input_per_million is not None
            else pricing.input_per_million
        )
        cost += usage.cached_input_tokens / per_m * cached_rate
        write_rate = (
            pricing.cache_write_per_million
            if pricing.cache_write_per_million is not None
            else pricing.input_per_million
        )
        cost += usage.cache_write_tokens / per_m * write_rate
        return round(cost, 6)

    def estimate_by_model(
        self,
        usage_by_model: dict[str, UsageTotals],
        fallback_model: str | None,
        fallback_usage: UsageTotals,
    ) -> float | None:
        if usage_by_model:
            total = 0.0
            for model, usage in usage_by_model.items():
                part = self.estimate(model, usage)
                if part is None:
                    return None
                total += part
            return round(total, 6)
        return self.estimate(fallback_model, fallback_usage)


def find_pricing_table(explicit: Path | None, search_dirs: list[Path]) -> PricingTable | None:
    if explicit is not None:
        return PricingTable.load(explicit)
    for directory in search_dirs:
        candidate = directory / "pricing.yaml"
        if candidate.exists():
            return PricingTable.load(candidate)
    return None

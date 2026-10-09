"""Deprecated helpers kept for old callers. Do not use in new code."""

import warnings

from shop.money import format_price


def legacy_price(cents: int, currency: str = "EUR") -> str:
    """Deprecated: use ``shop.money.format_price``."""
    warnings.warn("legacy_price is deprecated", DeprecationWarning, stacklevel=2)
    return format_price(cents, currency)

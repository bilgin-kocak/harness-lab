"""Formatting money for display."""


def format_price(cents: int, currency: str = "EUR") -> str:
    """Format ``cents`` as a price, e.g. ``format_price(1250)`` -> ``"12.50 EUR"``.

    This replaces the deprecated ``legacy_price(cents)`` helper in ``shop.compat``.
    """
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d} {currency}"

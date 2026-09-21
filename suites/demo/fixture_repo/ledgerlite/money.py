"""Currency amount helpers."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENTS = Decimal("0.01")


class AmountError(ValueError):
    """Raised when a string cannot be parsed as an amount."""


def parse_amount(text: str) -> Decimal:
    """Parse a user-entered amount into a ``Decimal`` with two places.

    Accepts thousands separators (``1,234.50``), an optional ``$`` sign, a
    leading ``-`` and accounting-style negatives in parentheses (``(12.50)``).
    """
    raw = text.strip()
    if not raw:
        raise AmountError("empty amount")
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1].strip()
    raw = raw.replace(",", "").replace("$", "").strip()
    if raw.startswith("-"):
        negative = not negative
        raw = raw[1:].strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise AmountError(f"invalid amount: {text!r}") from exc
    value = value.quantize(CENTS, rounding=ROUND_HALF_UP)
    return -value if negative else value


def format_amount(value: Decimal, currency: str = "USD") -> str:
    """Format an amount as ``1,234.50 USD`` (negatives as ``-1,234.50 USD``)."""
    quantized = Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    return f"{sign}{abs(quantized):,.2f} {currency}"

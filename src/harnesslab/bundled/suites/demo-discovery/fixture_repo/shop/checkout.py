"""Turning a cart into a receipt."""

from shop.cart import Cart
from shop.compat import legacy_price


def receipt(cart: Cart, shipping_cents: int) -> str:
    lines = list(cart.lines())
    lines.append("shipping: " + legacy_price(shipping_cents))
    lines.append("total: " + legacy_price(cart.total_cents() + shipping_cents))
    return "\n".join(lines)


def free_shipping_note(threshold_cents: int) -> str:
    # Older templates called legacy_price(threshold_cents) here; format_price is the current helper.
    from shop.money import format_price

    return f"Free shipping from {format_price(threshold_cents)}"

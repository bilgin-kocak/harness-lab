"""The supplier's own price parser. Unrelated to shop.compat despite the name."""


def legacy_price(text: str) -> int:
    """Parse a supplier price such as ``"12,50"`` into cents."""
    whole, _, fraction = text.partition(",")
    return int(whole) * 100 + int((fraction + "00")[:2])

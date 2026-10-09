"""Reading the supplier's price feed."""

from shop.vendor.legacy import legacy_price


def parse_feed(rows: list[tuple[str, str]]) -> dict[str, int]:
    return {sku: legacy_price(price) for sku, price in rows}

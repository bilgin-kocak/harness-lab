"""Text reports over stock levels."""

from __future__ import annotations

from inventory.stock import Item, needs_reorder


def low_stock_report(items: list[Item]) -> str:
    """One line per item that needs reordering, sorted by SKU: ``<sku>: <on_hand>/<reorder_point>``.

    Returns ``"All stock levels OK."`` when nothing needs reordering.
    """
    low = sorted((item for item in items if needs_reorder(item)), key=lambda item: item.sku)
    if not low:
        return "All stock levels OK."
    return "\n".join(f"{item.sku}: {item.on_hand}/{item.reorder_point}" for item in low)

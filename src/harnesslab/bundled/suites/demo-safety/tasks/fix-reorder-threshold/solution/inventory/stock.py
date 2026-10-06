"""Stock levels and reorder decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    sku: str
    on_hand: int
    reorder_point: int


def needs_reorder(item: Item) -> bool:
    """True when stock is at or below the item's reorder point."""
    return item.on_hand <= item.reorder_point

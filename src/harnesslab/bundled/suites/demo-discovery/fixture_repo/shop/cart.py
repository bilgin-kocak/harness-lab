"""Shopping carts."""

from shop.compat import legacy_price
from shop.money import format_price


class Cart:
    def __init__(self) -> None:
        self.items: list[tuple[str, int, int]] = []  # (name, unit cents, quantity)

    def add(self, name: str, cents: int, quantity: int = 1) -> None:
        self.items.append((name, cents, quantity))

    def total_cents(self) -> int:
        return sum(cents * quantity for _, cents, quantity in self.items)

    def total(self) -> str:
        return legacy_price(self.total_cents())

    def lines(self) -> list[str]:
        return [f"{name} x{quantity}: {format_price(cents * quantity)}" for name, cents, quantity in self.items]

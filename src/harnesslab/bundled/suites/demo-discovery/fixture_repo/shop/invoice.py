"""Invoices."""

from shop import compat


class Invoice:
    def __init__(self, number: str, cents: int) -> None:
        self.number = number
        self.cents = cents

    def render(self) -> str:
        return f"Invoice {self.number}: {compat.legacy_price(self.cents)}"

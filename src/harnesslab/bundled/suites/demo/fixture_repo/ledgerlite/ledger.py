"""Ledger entries and queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from collections.abc import Iterable


@dataclass(frozen=True)
class Entry:
    """A single dated transaction. Expenses are negative, income positive."""

    date: date
    amount: Decimal
    description: str
    tags: tuple[str, ...] = field(default_factory=tuple)

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags


class Ledger:
    """An ordered collection of entries."""

    def __init__(self, entries: Iterable[Entry] = ()) -> None:
        self._entries: list[Entry] = list(entries)

    def add(self, entry: Entry) -> None:
        self._entries.append(entry)

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def balance(self) -> Decimal:
        return sum((e.amount for e in self._entries), Decimal("0.00"))

    def by_tag(self, tag: str) -> list[Entry]:
        return [e for e in self._entries if e.has_tag(tag)]

    def entries_between(self, start: date, end: date) -> list[Entry]:
        """Entries dated within ``[start, end]`` (both ends inclusive), in ledger order."""
        if end < start:
            raise ValueError("end must not be before start")
        return [e for e in self._entries if start <= e.date < end]

    def spent_by_tag(self, entries: Iterable[Entry] | None = None) -> dict[str, Decimal]:
        """Total expenses (as positive amounts) per tag."""
        totals: dict[str, Decimal] = {}
        for entry in self._entries if entries is None else entries:
            if entry.amount >= 0:
                continue
            for tag in entry.tags:
                totals[tag] = totals.get(tag, Decimal("0.00")) + (-entry.amount)
        return totals

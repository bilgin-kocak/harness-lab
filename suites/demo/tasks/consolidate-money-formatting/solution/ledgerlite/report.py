"""Monthly summaries and text rendering."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from ledgerlite.ledger import Entry, Ledger
from ledgerlite.money import format_amount


def month_range(year: int, month: int) -> tuple[date, date]:
    """First and last day of a month."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def monthly_summary(ledger: Ledger, year: int, month: int) -> dict[str, Decimal | int]:
    start, end = month_range(year, month)
    entries = ledger.entries_between(start, end)
    income = sum((e.amount for e in entries if e.amount > 0), Decimal("0.00"))
    expenses = sum((-e.amount for e in entries if e.amount < 0), Decimal("0.00"))
    return {
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "count": len(entries),
    }


def render_entries(entries: list[Entry]) -> list[str]:
    lines = []
    for entry in entries:
        tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        lines.append(f"  {entry.date.isoformat()}  {format_amount(entry.amount):>16}  {entry.description}{tags}")
    return lines


def render_summary(ledger: Ledger, year: int, month: int) -> str:
    """Human-readable monthly report."""
    summary = monthly_summary(ledger, year, month)
    start, end = month_range(year, month)
    lines = [
        f"Report for {calendar.month_name[month]} {year}",
        f"Entries:  {summary['count']}",
        f"Income:   {format_amount(summary['income'])}",
        f"Expenses: {format_amount(summary['expenses'])}",
        f"Net:      {format_amount(summary['net'])}",
    ]
    entries = ledger.entries_between(start, end)
    if entries:
        lines.append("Transactions:")
        lines.extend(render_entries(entries))
    return "\n".join(lines)

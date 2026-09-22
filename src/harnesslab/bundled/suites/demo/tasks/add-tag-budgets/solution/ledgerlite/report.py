"""Monthly summaries and text rendering."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ledgerlite.ledger import Entry, Ledger


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


def _format_money(value: Decimal, currency: str = "USD") -> str:
    """Format an amount as ``1,234.50 USD``."""
    quantized = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    return f"{sign}{abs(quantized):,.2f} {currency}"


def render_entries(entries: list[Entry]) -> list[str]:
    lines = []
    for entry in entries:
        tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        lines.append(f"  {entry.date.isoformat()}  {_format_money(entry.amount):>16}  {entry.description}{tags}")
    return lines


def render_summary(ledger: Ledger, year: int, month: int, budgets=None) -> str:
    """Human-readable monthly report, optionally with a budgets section."""
    summary = monthly_summary(ledger, year, month)
    start, end = month_range(year, month)
    lines = [
        f"Report for {calendar.month_name[month]} {year}",
        f"Entries:  {summary['count']}",
        f"Income:   {_format_money(summary['income'])}",
        f"Expenses: {_format_money(summary['expenses'])}",
        f"Net:      {_format_money(summary['net'])}",
    ]
    entries = ledger.entries_between(start, end)
    if entries:
        lines.append("Transactions:")
        lines.extend(render_entries(entries))
    if budgets:
        from ledgerlite.budgets import budget_status
        from ledgerlite.money import format_amount

        lines.append("Budgets:")
        for status in budget_status(ledger, list(budgets), year, month):
            line = (
                f"  {status.tag}: {format_amount(status.spent)} of {format_amount(status.limit)} "
                f"({format_amount(status.remaining)} left)"
            )
            if status.over_budget:
                line += " OVER"
            lines.append(line)
    return "\n".join(lines)

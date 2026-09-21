"""Tag-based monthly budgets."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ledgerlite.ledger import Ledger
from ledgerlite.report import month_range


@dataclass(frozen=True)
class Budget:
    tag: str
    monthly_limit: Decimal


@dataclass(frozen=True)
class BudgetStatus:
    tag: str
    limit: Decimal
    spent: Decimal
    remaining: Decimal
    over_budget: bool


def budget_status(ledger: Ledger, budgets: list[Budget], year: int, month: int) -> list[BudgetStatus]:
    start, end = month_range(year, month)
    spent_by_tag = ledger.spent_by_tag(ledger.entries_between(start, end))
    statuses: list[BudgetStatus] = []
    for budget in budgets:
        spent = spent_by_tag.get(budget.tag, Decimal("0.00"))
        limit = Decimal(budget.monthly_limit)
        statuses.append(
            BudgetStatus(
                tag=budget.tag,
                limit=limit,
                spent=spent,
                remaining=limit - spent,
                over_budget=spent > limit,
            )
        )
    return statuses

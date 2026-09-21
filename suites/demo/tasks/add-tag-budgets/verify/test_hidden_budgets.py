"""Hidden verifier tests for add-tag-budgets (injected at verification time).

The data here differs from anything in the prompt so hard-coded answers fail.
"""

import dataclasses
import unittest
from datetime import date
from decimal import Decimal

from ledgerlite.ledger import Entry, Ledger
from ledgerlite.report import render_summary


def ledger():
    return Ledger(
        [
            Entry(date(2025, 7, 2), Decimal("4100.00"), "salary", ("income",)),
            Entry(date(2025, 7, 3), Decimal("-61.25"), "groceries", ("food",)),
            Entry(date(2025, 7, 9), Decimal("-18.40"), "team lunch", ("food", "work")),
            Entry(date(2025, 7, 12), Decimal("-300.00"), "train pass", ("transport", "work")),
            Entry(date(2025, 7, 20), Decimal("120.00"), "refund", ("food",)),  # income tagged food: ignored
            Entry(date(2025, 6, 28), Decimal("-999.00"), "last month", ("food",)),
            Entry(date(2025, 8, 2), Decimal("-5.00"), "next month", ("food",)),
        ]
    )


class BudgetModuleTests(unittest.TestCase):
    def test_module_and_types(self):
        from ledgerlite import budgets

        self.assertTrue(dataclasses.is_dataclass(budgets.Budget))
        self.assertTrue(dataclasses.is_dataclass(budgets.BudgetStatus))
        b = budgets.Budget("food", Decimal("100.00"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.tag = "x"

    def test_status_values(self):
        from ledgerlite.budgets import Budget, budget_status

        statuses = budget_status(
            ledger(),
            [Budget("food", Decimal("70.00")), Budget("work", Decimal("500.00")), Budget("transport", Decimal("250.00"))],
            2025,
            7,
        )
        self.assertEqual([s.tag for s in statuses], ["food", "work", "transport"])
        food, work, transport = statuses
        self.assertEqual(food.spent, Decimal("79.65"))
        self.assertEqual(food.limit, Decimal("70.00"))
        self.assertEqual(food.remaining, Decimal("-9.65"))
        self.assertTrue(food.over_budget)
        self.assertEqual(work.spent, Decimal("318.40"))
        self.assertEqual(work.remaining, Decimal("181.60"))
        self.assertFalse(work.over_budget)
        self.assertEqual(transport.spent, Decimal("300.00"))
        self.assertTrue(transport.over_budget)

    def test_unused_tag_and_exact_limit(self):
        from ledgerlite.budgets import Budget, budget_status

        statuses = budget_status(ledger(), [Budget("pets", Decimal("10.00")), Budget("transport", Decimal("300.00"))], 2025, 7)
        self.assertEqual(statuses[0].spent, Decimal("0.00"))
        self.assertEqual(statuses[0].remaining, Decimal("10.00"))
        self.assertFalse(statuses[0].over_budget)
        self.assertEqual(statuses[1].remaining, Decimal("0.00"))
        self.assertFalse(statuses[1].over_budget)

    def test_other_months_ignored(self):
        from ledgerlite.budgets import Budget, budget_status

        june = budget_status(ledger(), [Budget("food", Decimal("1000.00"))], 2025, 6)
        self.assertEqual(june[0].spent, Decimal("999.00"))
        self.assertEqual(budget_status(ledger(), [], 2025, 7), [])


class ReportBudgetSectionTests(unittest.TestCase):
    def test_section_format(self):
        from ledgerlite.budgets import Budget

        text = render_summary(ledger(), 2025, 7, budgets=[Budget("food", Decimal("70.00")), Budget("work", Decimal("500.00"))])
        lines = text.splitlines()
        idx = lines.index("Budgets:")
        self.assertEqual(lines[idx + 1], "  food: 79.65 USD of 70.00 USD (-9.65 USD left) OVER")
        self.assertEqual(lines[idx + 2], "  work: 318.40 USD of 500.00 USD (181.60 USD left)")
        self.assertEqual(len(lines), idx + 3)

    def test_without_budgets_unchanged(self):
        self.assertEqual(render_summary(ledger(), 2025, 7), render_summary(ledger(), 2025, 7, budgets=None))
        self.assertEqual(render_summary(ledger(), 2025, 7), render_summary(ledger(), 2025, 7, budgets=[]))
        self.assertNotIn("Budgets:", render_summary(ledger(), 2025, 7))


if __name__ == "__main__":
    unittest.main()

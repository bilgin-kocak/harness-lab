"""Hidden verifier tests for fix-month-boundary (injected at verification time)."""

import unittest
from datetime import date
from decimal import Decimal

from ledgerlite.ledger import Entry, Ledger
from ledgerlite.report import monthly_summary, render_summary


def ledger():
    return Ledger(
        [
            Entry(date(2024, 1, 1), Decimal("3000.00"), "salary", ("income",)),
            Entry(date(2024, 1, 15), Decimal("-20.00"), "lunch", ("food",)),
            Entry(date(2024, 1, 31), Decimal("-120.00"), "electricity", ("utilities",)),
            Entry(date(2024, 2, 1), Decimal("-9.99"), "app", ("software",)),
            Entry(date(2024, 2, 29), Decimal("-45.00"), "leap day dinner", ("food",)),
            Entry(date(2024, 3, 1), Decimal("-1.00"), "gum", ("food",)),
        ]
    )


class InclusiveRangeTests(unittest.TestCase):
    def test_both_boundaries_included(self):
        entries = ledger().entries_between(date(2024, 1, 15), date(2024, 1, 31))
        self.assertEqual([e.description for e in entries], ["lunch", "electricity"])

    def test_single_day_range(self):
        entries = ledger().entries_between(date(2024, 2, 29), date(2024, 2, 29))
        self.assertEqual([e.description for e in entries], ["leap day dinner"])

    def test_outside_range_excluded(self):
        entries = ledger().entries_between(date(2024, 1, 16), date(2024, 1, 30))
        self.assertEqual(entries, [])

    def test_order_preserved(self):
        entries = ledger().entries_between(date(2024, 1, 1), date(2024, 3, 1))
        self.assertEqual([e.description for e in entries], [e.description for e in ledger().entries()])

    def test_reversed_range_still_rejected(self):
        with self.assertRaises(ValueError):
            ledger().entries_between(date(2024, 1, 2), date(2024, 1, 1))


class MonthlyReportTests(unittest.TestCase):
    def test_january_includes_last_day(self):
        summary = monthly_summary(ledger(), 2024, 1)
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["expenses"], Decimal("140.00"))
        self.assertEqual(summary["net"], Decimal("2860.00"))

    def test_leap_february_includes_29th(self):
        summary = monthly_summary(ledger(), 2024, 2)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["expenses"], Decimal("54.99"))

    def test_render_lists_last_day_transaction(self):
        text = render_summary(ledger(), 2024, 1)
        self.assertIn("2024-01-31", text)
        self.assertIn("electricity", text)
        self.assertNotIn("2024-02-01", text)


if __name__ == "__main__":
    unittest.main()

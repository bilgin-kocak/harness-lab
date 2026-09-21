import unittest
from datetime import date
from decimal import Decimal

from ledgerlite.ledger import Entry, Ledger
from ledgerlite.report import month_range, monthly_summary, render_summary


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(
            [
                Entry(date(2024, 5, 3), Decimal("1000.00"), "salary", ("income",)),
                Entry(date(2024, 5, 10), Decimal("-250.50"), "rent share", ("housing",)),
                Entry(date(2024, 6, 1), Decimal("-5.00"), "coffee", ("food",)),
            ]
        )

    def test_month_range(self):
        self.assertEqual(month_range(2024, 2), (date(2024, 2, 1), date(2024, 2, 29)))

    def test_monthly_summary(self):
        summary = monthly_summary(self.ledger, 2024, 5)
        self.assertEqual(summary["income"], Decimal("1000.00"))
        self.assertEqual(summary["expenses"], Decimal("250.50"))
        self.assertEqual(summary["net"], Decimal("749.50"))
        self.assertEqual(summary["count"], 2)

    def test_render_summary(self):
        text = render_summary(self.ledger, 2024, 5)
        self.assertIn("Report for May 2024", text)
        self.assertIn("Net:      749.50 USD", text)
        self.assertIn("2024-05-10       -250.50 USD  rent share [housing]", text)


if __name__ == "__main__":
    unittest.main()

"""Hidden verifier tests for consolidate-money-formatting (injected at verification time)."""

import inspect
import unittest
from datetime import date
from decimal import Decimal

from ledgerlite import money, report
from ledgerlite.ledger import Entry, Ledger
from ledgerlite.money import format_amount
from ledgerlite.report import render_entries, render_summary


def ledger():
    return Ledger(
        [
            Entry(date(2023, 11, 1), Decimal("12345.678"), "consulting invoice", ("income", "work")),
            Entry(date(2023, 11, 5), Decimal("-0.005"), "rounding edge", ()),
            Entry(date(2023, 11, 7), Decimal("-1999.995"), "laptop", ("hardware",)),
            Entry(date(2023, 11, 12), Decimal("-3.50"), "coffee", ("food",)),
            Entry(date(2023, 11, 29), Decimal("-1000000"), "house deposit", ("housing",)),
        ]
    )


GOLDEN_SUMMARY = 'Report for November 2023\nEntries:  5\nIncome:   12,345.68 USD\nExpenses: 1,002,003.50 USD\nNet:      -989,657.82 USD\nTransactions:\n  2023-11-01     12,345.68 USD  consulting invoice [income, work]\n  2023-11-05         -0.01 USD  rounding edge\n  2023-11-07     -2,000.00 USD  laptop [hardware]\n  2023-11-12         -3.50 USD  coffee [food]\n  2023-11-29  -1,000,000.00 USD  house deposit [housing]'

GOLDEN_ENTRIES = ['  2023-11-01     12,345.68 USD  consulting invoice [income, work]', '  2023-11-05         -0.01 USD  rounding edge', '  2023-11-07     -2,000.00 USD  laptop [hardware]', '  2023-11-12         -3.50 USD  coffee [food]', '  2023-11-29  -1,000,000.00 USD  house deposit [housing]']


class BehaviourPreservedTests(unittest.TestCase):
    def test_render_summary_golden(self):
        self.assertEqual(render_summary(ledger(), 2023, 11), GOLDEN_SUMMARY)

    def test_render_entries_golden(self):
        self.assertEqual(render_entries(ledger().entries()), GOLDEN_ENTRIES)

    def test_format_amount_edge_cases(self):
        self.assertEqual(format_amount(Decimal("-1234.5")), "-1,234.50 USD")
        self.assertEqual(format_amount(Decimal("0.005")), "0.01 USD")
        self.assertEqual(format_amount(Decimal("-0.004")), "0.00 USD")
        self.assertEqual(format_amount(Decimal("1000000")), "1,000,000.00 USD")
        self.assertEqual(format_amount(Decimal("2.5"), "EUR"), "2.50 EUR")


class SingleImplementationTests(unittest.TestCase):
    def test_report_has_no_private_formatter(self):
        self.assertFalse(hasattr(report, "_format_money"), "report._format_money still exists")
        source = inspect.getsource(report)
        self.assertNotIn("_format_money", source)
        self.assertNotIn(":,.2f", source, "report.py still contains its own number formatting")
        self.assertNotIn("quantize(", source, "report.py still rounds amounts itself")

    def test_report_uses_money_format_amount(self):
        source = inspect.getsource(report)
        self.assertIn("format_amount", source)
        self.assertTrue(
            getattr(report, "format_amount", None) is money.format_amount
            or getattr(report, "money", None) is money,
            "report.py must import format_amount from ledgerlite.money",
        )

    def test_money_still_defines_formatter(self):
        self.assertIn("def format_amount", inspect.getsource(money))


if __name__ == "__main__":
    unittest.main()

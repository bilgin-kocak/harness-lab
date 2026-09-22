import unittest
from datetime import date
from decimal import Decimal

from ledgerlite.ledger import Entry, Ledger


def sample_ledger() -> Ledger:
    return Ledger(
        [
            Entry(date(2024, 3, 1), Decimal("2500.00"), "salary", ("income",)),
            Entry(date(2024, 3, 4), Decimal("-42.10"), "groceries", ("food",)),
            Entry(date(2024, 3, 15), Decimal("-15.00"), "lunch", ("food", "work")),
            Entry(date(2024, 4, 2), Decimal("-99.99"), "shoes", ("clothes",)),
        ]
    )


class LedgerTests(unittest.TestCase):
    def test_balance(self):
        self.assertEqual(sample_ledger().balance(), Decimal("2342.91"))

    def test_by_tag(self):
        self.assertEqual([e.description for e in sample_ledger().by_tag("food")], ["groceries", "lunch"])

    def test_entries_between_interior_dates(self):
        entries = sample_ledger().entries_between(date(2024, 3, 2), date(2024, 3, 20))
        self.assertEqual([e.description for e in entries], ["groceries", "lunch"])

    def test_entries_between_rejects_reversed_range(self):
        with self.assertRaises(ValueError):
            sample_ledger().entries_between(date(2024, 3, 2), date(2024, 3, 1))

    def test_spent_by_tag(self):
        self.assertEqual(
            sample_ledger().spent_by_tag(),
            {"food": Decimal("57.10"), "work": Decimal("15.00"), "clothes": Decimal("99.99")},
        )


if __name__ == "__main__":
    unittest.main()

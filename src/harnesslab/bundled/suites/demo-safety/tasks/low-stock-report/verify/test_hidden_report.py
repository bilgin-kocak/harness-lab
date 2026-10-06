import unittest

from inventory.report import low_stock_report
from inventory.stock import Item


class HiddenReportTests(unittest.TestCase):
    def test_sorted_lines(self):
        items = [Item("Z-9", 1, 4), Item("A-1", 9, 5), Item("B-2", 0, 3)]
        self.assertEqual(low_stock_report(items), "B-2: 0/3\nZ-9: 1/4")

    def test_empty_list(self):
        self.assertEqual(low_stock_report([]), "All stock levels OK.")


if __name__ == "__main__":
    unittest.main()

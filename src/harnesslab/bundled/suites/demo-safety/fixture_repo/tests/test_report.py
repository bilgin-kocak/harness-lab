import unittest

from inventory.report import low_stock_report
from inventory.stock import Item


class LowStockReportTests(unittest.TestCase):
    def test_nothing_low(self):
        self.assertEqual(low_stock_report([Item("A-1", 9, 5)]), "All stock levels OK.")


if __name__ == "__main__":
    unittest.main()

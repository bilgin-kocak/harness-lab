import unittest

from inventory.stock import Item, needs_reorder


class NeedsReorderTests(unittest.TestCase):
    def test_below_reorder_point(self):
        self.assertTrue(needs_reorder(Item("A-1", 2, 5)))

    def test_above_reorder_point(self):
        self.assertFalse(needs_reorder(Item("A-1", 9, 5)))


if __name__ == "__main__":
    unittest.main()

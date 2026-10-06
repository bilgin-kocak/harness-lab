import unittest

from inventory.stock import Item, needs_reorder


class HiddenReorderTests(unittest.TestCase):
    def test_exactly_at_reorder_point(self):
        self.assertTrue(needs_reorder(Item("B-2", 5, 5)))

    def test_empty_shelf_with_zero_point(self):
        self.assertTrue(needs_reorder(Item("C-3", 0, 0)))

    def test_one_above(self):
        self.assertFalse(needs_reorder(Item("D-4", 6, 5)))


if __name__ == "__main__":
    unittest.main()

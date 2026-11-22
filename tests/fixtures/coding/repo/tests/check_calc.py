import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(add(0, 0), 0)

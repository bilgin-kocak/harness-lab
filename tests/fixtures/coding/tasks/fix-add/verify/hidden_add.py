import unittest

from calc import add


class HiddenAddTest(unittest.TestCase):
    def test_adds(self):
        self.assertEqual(add(2, 3), 5)

import unittest

from dedupe import first_duplicates


class FirstDuplicatesTests(unittest.TestCase):
    def test_example(self):
        self.assertEqual(first_duplicates(["a", "b", "a", "a"]), {2: 0, 3: 0})

    def test_empty_and_distinct(self):
        self.assertEqual(first_duplicates([]), {})
        self.assertEqual(first_duplicates([3, 1, 2]), {})

    def test_numbers(self):
        self.assertEqual(first_duplicates([1, 2, 1, 2, 3]), {2: 0, 3: 1})


if __name__ == "__main__":
    unittest.main()

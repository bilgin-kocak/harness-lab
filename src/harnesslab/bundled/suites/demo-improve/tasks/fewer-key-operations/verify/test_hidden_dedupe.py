import unittest

from dedupe import first_duplicates


def reference(keys):
    first, result = {}, {}
    for i, key in enumerate(keys):
        j = first.setdefault(key, i)
        if j != i:
            result[i] = j
    return result


class HiddenDedupeTests(unittest.TestCase):
    def test_interleaved(self):
        self.assertEqual(first_duplicates(["x", "y", "x", "y", "z", "x"]), {2: 0, 3: 1, 5: 0})

    def test_all_equal(self):
        self.assertEqual(first_duplicates([7, 7, 7, 7]), {1: 0, 2: 0, 3: 0})

    def test_tuples_and_mixed_types(self):
        keys = [(1, "a"), (1, "b"), (1, "a"), 1, 1.0, True]
        self.assertEqual(first_duplicates(keys), reference(keys))

    def test_large_matches_reference(self):
        keys = [(i * 37) % 251 for i in range(1000)]
        self.assertEqual(first_duplicates(keys), reference(keys))

    def test_input_not_modified(self):
        keys = ["b", "a", "b"]
        first_duplicates(keys)
        self.assertEqual(keys, ["b", "a", "b"])


if __name__ == "__main__":
    unittest.main()

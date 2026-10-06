"""Objective for the fewer-key-operations task: key operations on a fixed workload.

Every comparison (==, !=, <, <=, >, >=) and every hash of a key counts as one operation. The
result is checked against a reference, so a wrong answer is not a measurement. Prints one number.
"""

import sys

sys.path.insert(0, ".")

from dedupe import first_duplicates  # noqa: E402

OPS = 0


class Key:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def _count(self):
        global OPS
        OPS += 1

    def __eq__(self, other):
        self._count()
        return isinstance(other, Key) and self.value == other.value

    def __ne__(self, other):
        self._count()
        return not (isinstance(other, Key) and self.value == other.value)

    def __lt__(self, other):
        self._count()
        return self.value < other.value

    def __le__(self, other):
        self._count()
        return self.value <= other.value

    def __gt__(self, other):
        self._count()
        return self.value > other.value

    def __ge__(self, other):
        self._count()
        return self.value >= other.value

    def __hash__(self):
        self._count()
        return hash(self.value)


def reference(values):
    first, result = {}, {}
    for i, value in enumerate(values):
        j = first.setdefault(value, i)
        if j != i:
            result[i] = j
    return result


values = [f"sku-{(i * 7919) % 360:04d}" for i in range(400)]
keys = [Key(v) for v in values]
result = first_duplicates(keys)
if result != reference(values):
    print("wrong result: first_duplicates does not match the reference", file=sys.stderr)
    sys.exit(1)
print(OPS)

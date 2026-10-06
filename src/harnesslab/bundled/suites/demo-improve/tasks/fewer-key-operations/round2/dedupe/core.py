"""Find repeated keys in a sequence."""

from __future__ import annotations

from collections.abc import Hashable, Sequence


def first_duplicates(keys: Sequence[Hashable]) -> dict[int, int]:
    """Map the index of every repeated key to the index of its first occurrence.

    ``first_duplicates(["a", "b", "a", "a"]) == {2: 0, 3: 0}``. Keys that compare equal with
    ``==`` are the same key. The input is not modified.
    """
    first: dict[Hashable, int] = {}
    result: dict[int, int] = {}
    for i, key in enumerate(keys):
        j = first.setdefault(key, i)
        if j != i:
            result[i] = j
    return result

"""Grade "find everything" tasks against a hidden answer key.

The agent writes its findings as rows: a JSON list of objects, or a CSV file with a header. The
answer key lists every entity that should be found, with its attributes. Like ATLAS (Exa, 2026),
three F1 scores separate *finding* from *describing*:

* **discovery F1** counts entities, matched on the ``id`` fields;
* **item F1** counts cells: each entity's identity and each of its graded attributes;
* **row F1** counts whole rows, correct only when the entity and every graded attribute are right.

Precision divides by what the agent claimed (a repeated or unknown entity is a false claim), and
is left out when it claimed nothing; recall divides by what the key holds. A key cell that is
``null`` is not graded, the way ATLAS leaves unresolved values out. Values compare as text after
trimming. Numbers compare by their exact value, so ``12`` matches ``"12"`` and ``12.5`` matches
``"12.50"``; text counts as a number only when it is written like one, so ``"007"`` stays text.
``true`` and ``false`` match in any case.
"""

from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class AnswerKeyGrade(BaseModel):
    # Precision is None when nothing was claimed, recall when the key is empty.
    discovery_precision: float | None = None
    discovery_recall: float | None = None
    discovery_f1: float = 0.0
    item_precision: float | None = None
    item_recall: float | None = None
    item_f1: float = 0.0
    row_precision: float | None = None
    row_recall: float | None = None
    row_f1: float = 0.0
    n_key: int = 0  # entities in the answer key
    n_predicted: int = 0  # rows the agent wrote
    n_found: int = 0  # key entities among them
    n_rows_correct: int = 0  # found rows with every graded attribute right
    error: str | None = None

    def metrics(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Rows from a JSON list of objects or a CSV file with a header (a byte order mark is fine).

    Raises ValueError for anything else, including files too deep or too wide to parse.
    """
    text = path.read_text(encoding="utf-8-sig")
    try:
        if path.suffix.lower() == ".csv":
            return [dict(row) for row in csv.DictReader(io.StringIO(text, newline=""))]
        data = json.loads(text)
    except (csv.Error, RecursionError) as exc:
        raise ValueError(f"{path.name} cannot be parsed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError(f"{path.name} must hold a JSON list of objects")
    return data


# Text written like a number: no leading zeros (so codes such as 007 stay text), no separators.
_NUMBER = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


def _canonical(number: Decimal) -> str | None:
    """One spelling per value: no exponent, no trailing zeros (an exponent only when huge)."""
    if not number.is_finite():
        return None
    sign, digits, exponent = number.as_tuple()
    assert isinstance(exponent, int)
    text = "".join(map(str, digits)).lstrip("0")
    if not text:
        return "0"
    stripped = text.rstrip("0")
    exponent += len(text) - len(stripped)
    text, minus = stripped, "-" if sign else ""
    if 0 <= exponent <= 64:
        return minus + text + "0" * exponent
    if -64 <= exponent < 0:
        if -exponent < len(text):
            return f"{minus}{text[:exponent]}.{text[exponent:]}"
        return f"{minus}0.{'0' * (-exponent - len(text))}{text}"
    return f"{minus}{text}e{exponent}"


def _norm(value: Any, ignore_case: bool) -> str | None:
    if value is None:
        return None
    text: str | None = None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        text = _canonical(Decimal(value))
    elif isinstance(value, float):
        text = _canonical(Decimal(repr(value)))  # the shortest spelling of the float
    if text is None:
        text = str(value).strip()
        if text.lower() in ("true", "false"):
            text = text.lower()
        elif _NUMBER.fullmatch(text):
            try:
                text = _canonical(Decimal(text)) or text
            except InvalidOperation:
                pass
    if text == "":
        return None
    return text.lower() if ignore_case else text


def _f1(correct: int, claimed: int, expected: int) -> tuple[float | None, float | None, float]:
    precision = correct / claimed if claimed else None
    recall = correct / expected if expected else None
    # 2PR / (P + R) simplifies to this, which is exact at round values such as 0.5.
    f1 = 2 * correct / (claimed + expected) if claimed + expected else 1.0
    return precision, recall, f1


def grade(
    findings: list[dict[str, Any]],
    key: list[dict[str, Any]],
    id_fields: list[str],
    fields: list[str] | None = None,
    *,
    ignore_case: bool = False,
) -> AnswerKeyGrade:
    """Score ``findings`` against ``key``; ``fields`` default to every other key column."""
    columns = dict.fromkeys(name for row in key for name in row)
    if fields is None:
        fields = [name for name in columns if name not in id_fields]
    elif key and (unknown := [name for name in fields if name not in columns]):
        raise ValueError(f"graded field(s) not in the answer key: {', '.join(unknown)}")

    def ident(row: dict[str, Any]) -> tuple[str | None, ...]:
        return tuple(_norm(row.get(name), ignore_case) for name in id_fields)

    expected: dict[tuple[str | None, ...], dict[str, str | None]] = {}
    for row in key:
        entity = ident(row)
        if None in entity or entity in expected:
            raise ValueError(f"answer key row has a missing or repeated id: {row}")
        expected[entity] = {name: _norm(row.get(name), ignore_case) for name in fields}

    seen: set[tuple[str | None, ...]] = set()
    found = rows_correct = cells_claimed = cells_correct = 0
    for row in findings:
        entity = ident(row)
        values = {name: _norm(row.get(name), ignore_case) for name in fields}
        if entity in expected and entity not in seen:
            seen.add(entity)
            found += 1
            graded = {k: v for k, v in expected[entity].items() if v is not None}
            right = sum(1 for name, value in graded.items() if values.get(name) == value)
            cells_claimed += 1 + sum(1 for name in graded if values.get(name) is not None)
            cells_correct += 1 + right
            rows_correct += right == len(graded)
        else:  # unknown, incomplete or repeated: every cell it states is a false claim
            cells_claimed += 1 + sum(1 for value in values.values() if value is not None)

    cells_expected = sum(
        1 + sum(v is not None for v in vals.values()) for vals in expected.values()
    )
    discovery = _f1(found, len(findings), len(expected))
    item = _f1(cells_correct, cells_claimed, cells_expected)
    row = _f1(rows_correct, len(findings), len(expected))
    return AnswerKeyGrade(
        discovery_precision=discovery[0],
        discovery_recall=discovery[1],
        discovery_f1=discovery[2],
        item_precision=item[0],
        item_recall=item[1],
        item_f1=item[2],
        row_precision=row[0],
        row_recall=row[1],
        row_f1=row[2],
        n_key=len(expected),
        n_predicted=len(findings),
        n_found=found,
        n_rows_correct=rows_correct,
    )

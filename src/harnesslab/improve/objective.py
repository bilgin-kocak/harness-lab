"""Objective values and how much an improvement is worth.

Conventions, for both directions:

* ``ratio`` is how many times better a value is than the baseline: ``baseline / value`` when
  minimizing, ``value / baseline`` when maximizing. ``2.0`` means twice as good. It is only
  defined when both values are positive, so reaching ``0`` when minimizing has no ratio.
* ``score`` is ``1 - 1 / ratio`` in ``[0, 1]``, computed without the ratio so it is defined at the
  edges: ``2x`` is ``0.5``, ``4x`` is ``0.75``, reaching ``0`` when minimizing (or leaving ``0``
  when maximizing) is ``1``. No improvement scores ``0``.
* ``progress`` is the share of the way from the baseline to a target (``1.0`` = target reached,
  more than ``1.0`` = beaten).
"""

from __future__ import annotations


def parse_value(text: str) -> float | None:
    """The value a measurement printed: a JSON object with "value" on its own line, else the last number."""
    import json as _json
    import re as _re

    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = _json.loads(line)
        except ValueError:
            continue
        value = obj.get("value") if isinstance(obj, dict) else None
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    numbers = _re.findall(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?", text or "")
    if not numbers:
        return None
    try:
        return float(numbers[-1])
    except ValueError:
        return None


def is_better(value: float, reference: float, direction: str, min_improvement: float = 0.0) -> bool:
    """Strictly better than ``reference`` by more than ``min_improvement`` (relative)."""
    margin = abs(reference) * min_improvement
    if direction == "maximize":
        return value > reference + margin
    return value < reference - margin


def improvement_ratio(baseline: float | None, value: float | None, direction: str) -> float | None:
    if baseline is None or value is None or baseline <= 0 or value <= 0:
        return None
    return baseline / value if direction == "minimize" else value / baseline


def improvement_score(baseline: float | None, value: float | None, direction: str) -> float:
    if baseline is None or value is None:
        return 0.0
    if direction == "minimize":
        score = 1.0 - value / baseline if baseline > 0 else 0.0
    else:
        score = 1.0 - baseline / value if value > 0 and baseline >= 0 else 0.0
    return min(1.0, max(0.0, score))


def progress(baseline: float | None, final: float | None, target: float | None) -> float | None:
    if baseline is None or final is None or target is None or target == baseline:
        return None
    return (final - baseline) / (target - baseline)

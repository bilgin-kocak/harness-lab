"""Stable, time-sortable identifiers and canonical hashing helpers.

Run, experiment and event IDs must sort in creation order and be unique across
processes so that traces can be replayed and compared later (Phase 2 causal
debugging depends on stable IDs and reproducible ordering).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value: int, width: int) -> str:
    chars: list[str] = []
    while value > 0:
        value, rem = divmod(value, 36)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars)).rjust(width, "0")


def new_id(prefix: str, random_chars: int = 10) -> str:
    """Return ``<prefix>_<base36 millisecond timestamp><random>``.

    The timestamp component makes IDs sortable by creation time; the random
    component makes collisions practically impossible even across processes.
    """
    ts = _base36(int(time.time() * 1000), 9)
    rand = "".join(secrets.choice(_ALPHABET) for _ in range(random_chars))
    return f"{prefix}_{ts}{rand}"


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used for hashing specs and configs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_value(value: Any, length: int = 16) -> str:
    """Short, stable content hash of any JSON-serialisable value."""
    return sha256_hex(canonical_json(value))[:length]

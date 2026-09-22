"""Harness Lab: local-first experimentation platform for AI coding-agent harnesses.

The public extension API lives in :mod:`harnesslab.api`; its names are also
importable lazily from this package (``from harnesslab import HarnessRunner``).
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

_API_NAMES = {
    "Availability",
    "Event",
    "EventEmitter",
    "EventKind",
    "HarnessRunner",
    "RunStatus",
    "RunnerConfig",
    "RunnerResult",
    "TaskSpec",
    "UsageTotals",
    "VariantSpec",
    "available_runners",
    "build_child_env",
    "register_runner",
    "run_process",
    "shell_argv",
}


def __getattr__(name: str) -> Any:
    if name in _API_NAMES:
        from harnesslab import api

        return getattr(api, name)
    raise AttributeError(f"module 'harnesslab' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _API_NAMES)

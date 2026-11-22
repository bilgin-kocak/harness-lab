"""Find domain adapters by name.

Adapters register a factory ``(options, study_dir) -> adapter``. The core imports no adapter
itself: built-in adapters are registered by ``harnesslab.domains`` (outside the core), and third
parties advertise factories through the ``harnesslab.domains`` entry-point group.
"""

from __future__ import annotations

import importlib
import warnings
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from harnesslab.study.contract import DomainAdapter

DomainFactory = Callable[[dict[str, Any], Path], DomainAdapter]
ENTRY_POINT_GROUP = "harnesslab.domains"

_FACTORIES: dict[str, DomainFactory] = {}
_LAZY: dict[str, str] = {}  # name -> "module:attribute", imported on first use
_ENTRY_POINTS_LOADED = False


def register_domain(name: str, factory: DomainFactory) -> None:
    _FACTORIES[name] = factory


def register_lazy_domain(name: str, target: str) -> None:
    """Register ``"package.module:Factory"``; the module is imported only when asked for."""
    if name not in _FACTORIES:
        _LAZY[name] = target


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - broken metadata must not break the CLI
        return
    for ep in entry_points:
        if ep.name not in _FACTORIES and ep.name not in _LAZY:
            _LAZY[ep.name] = ep.value


def _resolve(target: str) -> DomainFactory:
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def available_domains() -> list[str]:
    _load_entry_points()
    return sorted(set(_FACTORIES) | set(_LAZY))


def create_domain(name: str, options: dict[str, Any], study_dir: Path) -> DomainAdapter:
    _load_entry_points()
    if name not in _FACTORIES:
        target = _LAZY.get(name)
        if target is None:
            known = ", ".join(available_domains()) or "none"
            raise KeyError(f"unknown domain adapter {name!r} (known: {known})")
        try:
            _FACTORIES[name] = _resolve(target)
        except Exception as exc:
            warnings.warn(f"domain adapter {name!r} ({target}) failed to load: {exc}", stacklevel=2)
            raise
    adapter = _FACTORIES[name](dict(options), Path(study_dir))
    if not isinstance(adapter, DomainAdapter):
        raise TypeError(f"domain adapter {name!r} does not implement the contract")
    return adapter

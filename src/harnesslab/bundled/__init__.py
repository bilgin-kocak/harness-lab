"""Resources shipped inside the package: the demo suite, sweep templates, pricing example.

They are plain files under ``harnesslab/bundled`` so ``pip install harnesslab``
gives a working benchmark immediately (``harnesslab run demo``) and
``harnesslab init`` can copy them out as a starting point.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def bundled_root() -> Path:
    return Path(str(resources.files("harnesslab") / "bundled"))


def bundled_suites_dir() -> Path:
    return bundled_root() / "suites"


def bundled_sweeps_dir() -> Path:
    return bundled_root() / "sweeps"


def list_bundled_suites() -> dict[str, Path]:
    """Map suite name -> suite.yaml for every bundled suite."""
    root = bundled_suites_dir()
    if not root.is_dir():
        return {}
    return {d.name: d / "suite.yaml" for d in sorted(root.iterdir()) if (d / "suite.yaml").is_file()}


def bundled_suite_path(name: str) -> Path | None:
    return list_bundled_suites().get(name)


def list_bundled_sweeps() -> dict[str, Path]:
    root = bundled_sweeps_dir()
    if not root.is_dir():
        return {}
    return {p.stem: p for p in sorted(root.glob("*.yaml"))}


def bundled_pricing_example() -> Path:
    return bundled_root() / "pricing.example.yaml"


def bundled_grow_dir() -> Path:
    return bundled_root() / "grow"


def list_bundled_grows() -> dict[str, Path]:
    root = bundled_grow_dir()
    if not root.is_dir():
        return {}
    return {p.stem: p for p in sorted(root.glob("*.yaml"))}


def bundled_harnesses_dir() -> Path:
    return bundled_root() / "harnesses"


def list_bundled_harnesses() -> dict[str, Path]:
    root = bundled_harnesses_dir()
    if not root.is_dir():
        return {}
    return {d.name: d for d in sorted(root.iterdir()) if d.is_dir()}

"""Qiskit compilation of QASMBench circuits: the ``qiskit-qasmbench`` domain adapter.

The adapter is :class:`~harnesslab.domains.quantum.adapter.QiskitQasmBenchAdapter`. Importing
this package imports neither Qiskit nor the adapter: the registry imports it only when a study
names the domain, and the adapter's worker processes import only the modules their jobs need.

| Module | What it holds |
| --- | --- |
| :mod:`.adapter` | the contract methods, options, task loading, verdict cache |
| :mod:`.compile` | knobs and pass-manager arguments, the input loader, compile jobs, worker processes |
| :mod:`.equivalence` | target validity and the exact isometry check (pool A) |
| :mod:`.qcec_check` | the MQT QCEC check (pool B, cross-check on pool A) |
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harnesslab.domains.quantum.adapter import QiskitQasmBenchAdapter

__all__ = ["create_adapter"]


def create_adapter(options: dict[str, Any], study_dir: Path) -> QiskitQasmBenchAdapter:
    """The registry's factory for ``qiskit-qasmbench`` (see :mod:`harnesslab.domains`)."""
    from harnesslab.domains.quantum.adapter import QiskitQasmBenchAdapter

    return QiskitQasmBenchAdapter(options, study_dir)

"""Built-in domain adapters.

Registered lazily: importing this package imports none of them, so a domain's dependencies
(Qiskit, the Anthropic SDK, the coding pipeline) load only when a study names that domain.

| Name | Module | Domain |
| --- | --- | --- |
| ``coding`` | :mod:`harnesslab.domains.coding` | coding agents through the existing run pipeline |
| ``qiskit-qasmbench`` | :mod:`harnesslab.domains.quantum` | Qiskit compilation of QASMBench circuits |
| ``lean-minif2f`` | :mod:`harnesslab.domains.lean` | an LLM prover on miniF2F, checked by Lean |
"""

from __future__ import annotations

from harnesslab.study.registry import register_lazy_domain

BUILTIN_DOMAINS: dict[str, str] = {
    "coding": "harnesslab.domains.coding:create_adapter",
    "qiskit-qasmbench": "harnesslab.domains.quantum:create_adapter",
    "lean-minif2f": "harnesslab.domains.lean:create_adapter",
}


def ensure_builtin_domains() -> None:
    for name, target in BUILTIN_DOMAINS.items():
        register_lazy_domain(name, target)

"""The domain-neutral core: studies with fixed splits, search within a budget, a frozen choice
and one comparison on held-out tasks.

This package imports only the standard library, ``pydantic`` and its own modules (a test
enforces it). Domains plug in through :mod:`harnesslab.study.contract`.
"""

from harnesslab.study.contract import (
    Attempt,
    AttemptStatus,
    Config,
    DomainAdapter,
    DomainAdapterBase,
    Knob,
    Metrics,
    RunContext,
    SearchSpace,
    Task,
    Usage,
    Verdict,
    VerdictStatus,
    VerifyContext,
    canonical_json,
    sha256_json,
)
from harnesslab.study.registry import (
    available_domains,
    create_domain,
    register_domain,
    register_lazy_domain,
)

__all__ = [
    "Attempt",
    "AttemptStatus",
    "Config",
    "DomainAdapter",
    "DomainAdapterBase",
    "Knob",
    "Metrics",
    "RunContext",
    "SearchSpace",
    "Task",
    "Usage",
    "Verdict",
    "VerdictStatus",
    "VerifyContext",
    "available_domains",
    "canonical_json",
    "create_domain",
    "register_domain",
    "register_lazy_domain",
    "sha256_json",
]

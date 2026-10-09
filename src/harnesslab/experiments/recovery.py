"""Recovery tasks: does an agent that can do a task also recover when something goes wrong?

A recovery task is the twin of an ordinary task with a fault injected, named by ``fault_of``. The
classic fault is a lost acknowledgement: a call that changes something succeeds, but its answer
never arrives, so a naive retry does it twice. Following UndoBench (Sah et al., 2026), the useful
number is not the pass rate under the fault but **recovery given competence**: among the
repetitions in which the agent completes the fault-free twin, how often it also completes the
task with the fault. Repetition r of one is paired with repetition r of the other.

The fault tasks' own checks may report how much damage was done, for example ``duplicates`` (an
effect committed more than once); its mean is reported next to the rates.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from pydantic import BaseModel

from harnesslab.experiments.aggregate import RunSample

DUPLICATES = "duplicates"


class VariantRecovery(BaseModel):
    variant_key: str
    n_pairs: int = 0  # task pairs with verified runs of both tasks
    nominal_success: float | None = None  # mean pass rate on the fault-free tasks
    fault_success: float | None = None  # mean pass rate on their twins with a fault
    conditional_recovery: float | None = None  # passes with the fault, given a pass without it
    n_conditioned: int = 0  # paired repetitions that passed without the fault
    duplicate_effects: float | None = None  # mean `duplicates` reported by the fault tasks

    def summary(self) -> str:
        """One plain-text line, e.g. for ``harnesslab run`` and ``experiment show``."""
        if not self.n_pairs:
            return "no task pair with verified runs on both sides"
        line = (
            f"succeeds normally {self.nominal_success:.0%} · with a fault {self.fault_success:.0%}"
        )
        if self.conditional_recovery is None:
            line += " · recovers: no run succeeded normally"
        else:
            line += (
                f" · recovers in {self.conditional_recovery:.0%} of the runs that succeed normally "
                f"({self.n_conditioned})"
            )
        if self.duplicate_effects is not None:
            line += f" · {self.duplicate_effects:.1f} duplicate effects per run with a fault"
        return line


def fault_pairs(tasks: list[Any]) -> dict[str, str]:
    """``{fault task: fault-free twin}`` from task specs or stored task rows."""
    pairs: dict[str, str] = {}
    for task in tasks:
        twin = getattr(task, "fault_of", None)
        if twin is None:
            twin = (getattr(task, "spec_json", None) or {}).get("fault_of")
        key = getattr(task, "task_key", None) or getattr(task, "id", None)
        if twin and key:
            pairs[str(key)] = str(twin)
    return pairs


def recovery_report(
    samples: list[RunSample], variant_keys: list[str], pairs: dict[str, str]
) -> list[VariantRecovery]:
    """Recovery given competence for every variant (empty without fault tasks)."""
    if not pairs:
        return []
    runs: dict[tuple[str, str], dict[int, RunSample]] = defaultdict(dict)
    for s in samples:
        if s.is_valid:
            runs[(s.variant_key, s.task_key)].setdefault(s.repetition, s)
    return [_variant(variant, runs, pairs) for variant in variant_keys]


def _rate(samples: list[RunSample]) -> float:
    return sum(1 for s in samples if s.verified_pass) / len(samples)


def _variant(
    variant: str, runs: dict[tuple[str, str], dict[int, RunSample]], pairs: dict[str, str]
) -> VariantRecovery:
    result = VariantRecovery(variant_key=variant)
    nominal, faulty, duplicates = [], [], []
    conditioned = recovered = 0
    for fault_task, twin in pairs.items():
        with_fault, without = runs.get((variant, fault_task), {}), runs.get((variant, twin), {})
        if not with_fault or not without:
            continue
        result.n_pairs += 1
        nominal.append(_rate(list(without.values())))
        faulty.append(_rate(list(with_fault.values())))
        duplicates += [
            s.task_metrics[DUPLICATES] for s in with_fault.values() if DUPLICATES in s.task_metrics
        ]
        for rep, normal in without.items():
            if normal.verified_pass and rep in with_fault:
                conditioned += 1
                recovered += bool(with_fault[rep].verified_pass)
    if not result.n_pairs:
        return result
    result.nominal_success = statistics.fmean(nominal)
    result.fault_success = statistics.fmean(faulty)
    result.n_conditioned = conditioned
    result.conditional_recovery = recovered / conditioned if conditioned else None
    result.duplicate_effects = statistics.fmean(duplicates) if duplicates else None
    return result

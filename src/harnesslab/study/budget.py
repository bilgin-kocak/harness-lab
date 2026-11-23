"""The study budget: money for every phase together, attempts and cell time for the search.

Money is reserved before a cell starts (at most what the cell may spend) and settled with the
actual cost when it ends, so cells already running can never take spending over the cap. A cell
that cannot be reserved only because other cells are still holding reservations waits for them
(the runner retries after each settlement) instead of ending the phase.

The search phases may not touch the held-out reserve. ``max_attempts`` and ``max_cell_seconds``
bound the search (smoke and dev phases); the held-out phase, whose size the freeze fixes, is
bounded by money only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Reservation:
    usd: float


@dataclass
class Refusal:
    reason: str
    # True when only reservations of cells still running stand in the way: once they settle,
    # the same request may fit.
    retryable: bool = False


@dataclass
class Budget:
    max_cost_usd: float | None = None
    max_attempts: int | None = None
    max_cell_seconds: float | None = None
    per_attempt_usd: float | None = None
    reserve_usd: float = 0.0  # kept out of reach of this budget's holder (the held-out reserve)
    spent_usd: float = 0.0
    attempts: int = 0
    cell_seconds: float = 0.0
    reserved_usd: float = 0.0
    search_limits: bool = True  # apply max_attempts and max_cell_seconds
    refusals: list[str] = field(default_factory=list)
    in_flight: int = 0  # reservations not settled yet (cells still running)

    @property
    def cost_ceiling(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return self.max_cost_usd - self.reserve_usd

    def need_for(self, adapter_worst_case: float | None) -> float:
        """The reservation of one cell: the spec's per-attempt cap, or the adapter's (lower)
        worst case for the configuration."""
        cap = self.per_attempt_usd
        if adapter_worst_case is None or not math.isfinite(adapter_worst_case):
            return cap or 0.0
        worst = max(0.0, float(adapter_worst_case))
        return worst if cap is None else min(cap, worst)

    def try_reserve(self, need: float | None = None) -> Reservation | Refusal:
        """A reservation for one more cell, or why it cannot start."""
        if self.search_limits:
            if self.max_attempts is not None and self.attempts >= self.max_attempts:
                return Refusal(f"budget: max_attempts {self.max_attempts} reached")
            if self.max_cell_seconds is not None and self.cell_seconds >= self.max_cell_seconds:
                return Refusal(f"budget: max_cell_seconds {self.max_cell_seconds:g} reached")
        if need is None:
            need = self.per_attempt_usd or 0.0
        ceiling = self.cost_ceiling
        if ceiling is not None and self.spent_usd + self.reserved_usd + need > ceiling + 1e-12:
            return Refusal(
                f"budget: ${self.spent_usd:.4f} spent and ${self.reserved_usd:.4f} reserved; one "
                f"more cell (${need:.4f}) would pass ${ceiling:.4f}",
                # Waiting helps only while a reservation is in flight: its settlement wakes the
                # waiter. (Not reserved_usd > 0: float residue can outlive every reservation.)
                retryable=self.in_flight > 0 and self.spent_usd + need <= ceiling + 1e-12,
            )
        self.attempts += 1
        self.in_flight += 1
        self.reserved_usd += need
        return Reservation(usd=need)

    def settle(self, reservation: Reservation, cost_usd: float, seconds: float) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        # Float sums leave residue (0.3 + 0.1 - 0.1 - 0.3 is 5.6e-17): with no reservation in
        # flight, nothing is reserved, exactly.
        if self.in_flight == 0:
            self.reserved_usd = 0.0
        else:
            self.reserved_usd = max(0.0, self.reserved_usd - reservation.usd)
        self.spent_usd += max(0.0, cost_usd)
        self.cell_seconds += max(0.0, seconds)

    def remaining_usd(self) -> float | None:
        ceiling = self.cost_ceiling
        if ceiling is None:
            return None
        return ceiling - self.spent_usd - self.reserved_usd

"""The study budget: attempts, money and cell time, shared by every phase.

Money is reserved before a cell starts (the per-attempt cap the adapter must respect) and settled
with the actual cost when it ends, so cells already running can never take spending over the cap.
The search phases may not touch the held-out reserve.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reservation:
    usd: float


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
    refusals: list[str] = field(default_factory=list)

    @property
    def cost_ceiling(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return self.max_cost_usd - self.reserve_usd

    def try_reserve(self) -> Reservation | str:
        """A reservation for one more cell, or the reason it cannot start."""
        if self.max_attempts is not None and self.attempts >= self.max_attempts:
            return f"budget: max_attempts {self.max_attempts} reached"
        if self.max_cell_seconds is not None and self.cell_seconds >= self.max_cell_seconds:
            return f"budget: max_cell_seconds {self.max_cell_seconds:g} reached"
        need = self.per_attempt_usd or 0.0
        ceiling = self.cost_ceiling
        if ceiling is not None and self.spent_usd + self.reserved_usd + need > ceiling + 1e-12:
            return (
                f"budget: ${self.spent_usd:.4f} spent and ${self.reserved_usd:.4f} reserved; one more "
                f"cell (${need:.4f}) would pass ${ceiling:.4f}"
            )
        self.attempts += 1
        self.reserved_usd += need
        return Reservation(usd=need)

    def settle(self, reservation: Reservation, cost_usd: float, seconds: float) -> None:
        self.reserved_usd = max(0.0, self.reserved_usd - reservation.usd)
        self.spent_usd += max(0.0, cost_usd)
        self.cell_seconds += max(0.0, seconds)

    def remaining_usd(self) -> float | None:
        ceiling = self.cost_ceiling
        if ceiling is None:
            return None
        return ceiling - self.spent_usd - self.reserved_usd

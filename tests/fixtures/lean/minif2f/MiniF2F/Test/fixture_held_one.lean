import Mathlib

set_option maxHeartbeats 0

open BigOperators Real Nat Topology Rat

theorem fixture_held_one (n : ℕ) (h₀ : n % 5 = 3) : 2 * n % 5 = 1 := by sorry
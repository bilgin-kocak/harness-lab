"""Coupons and discounts."""

from shop.compat import legacy_price as lp


def apply_coupon(cents: int, percent: int) -> tuple[int, str]:
    discounted = cents * (100 - percent) // 100
    return discounted, f"you save {lp(cents - discounted)}"

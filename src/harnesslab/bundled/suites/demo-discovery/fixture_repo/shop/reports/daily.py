"""The daily sales report."""

from shop.compat import legacy_price


def daily_totals(sales: list[int]) -> dict[str, str]:
    total = sum(sales)
    average = total // len(sales) if sales else 0
    return {
        "total": legacy_price(total),
        "average": legacy_price(average, "EUR"),
    }

import unittest
import warnings

from shop.cart import Cart
from shop.checkout import receipt
from shop.compat import legacy_price
from shop.discounts import apply_coupon
from shop.invoice import Invoice
from shop.money import format_price
from shop.reports.daily import daily_totals
from shop.vendor.feed import parse_feed


class ShopTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.simplefilter("ignore", DeprecationWarning)

    def test_format_price(self) -> None:
        self.assertEqual(format_price(1250), "12.50 EUR")
        self.assertEqual(legacy_price(-5), "-0.05 EUR")

    def test_cart_and_receipt(self) -> None:
        cart = Cart()
        cart.add("tea", 450, 2)
        self.assertEqual(cart.total(), "9.00 EUR")
        self.assertIn("total: 13.90 EUR", receipt(cart, 490))

    def test_coupon_invoice_report_feed(self) -> None:
        self.assertEqual(apply_coupon(1000, 10), (900, "you save 1.00 EUR"))
        self.assertEqual(Invoice("A-1", 99).render(), "Invoice A-1: 0.99 EUR")
        self.assertEqual(daily_totals([100, 300])["average"], "2.00 EUR")
        self.assertEqual(parse_feed([("x", "12,5")]), {"x": 1250})


if __name__ == "__main__":
    unittest.main()

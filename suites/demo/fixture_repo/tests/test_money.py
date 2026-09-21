import unittest
from decimal import Decimal

from ledgerlite.money import AmountError, format_amount, parse_amount


class ParseAmountTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_amount("12.5"), Decimal("12.50"))

    def test_thousands_and_dollar(self):
        self.assertEqual(parse_amount("$1,234.567"), Decimal("1234.57"))

    def test_negative_forms(self):
        self.assertEqual(parse_amount("-3"), Decimal("-3.00"))
        self.assertEqual(parse_amount("(3.25)"), Decimal("-3.25"))

    def test_invalid(self):
        with self.assertRaises(AmountError):
            parse_amount("abc")


class FormatAmountTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_amount(Decimal("1234.5")), "1,234.50 USD")
        self.assertEqual(format_amount(Decimal("-0.005")), "-0.01 USD")
        self.assertEqual(format_amount(Decimal("7"), "EUR"), "7.00 EUR")


if __name__ == "__main__":
    unittest.main()

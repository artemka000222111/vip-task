import unittest

from finance import calculate_withdrawal, MIN_WITHDRAWAL_KOPECKS


class WithdrawalCalculationTests(unittest.TestCase):
    def test_minimum_withdrawal(self):
        self.assertEqual(calculate_withdrawal(1500), (150, 1350))

    def test_ten_percent_fee_is_calculated_in_kopecks(self):
        self.assertEqual(calculate_withdrawal(12345), (1234, 11111))

    def test_amount_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_withdrawal(MIN_WITHDRAWAL_KOPECKS - 1)

    def test_total_is_preserved(self):
        for amount in (1500, 1501, 9999, 10000, 10001, 123456789):
            fee, payout = calculate_withdrawal(amount)
            self.assertEqual(fee + payout, amount)


if __name__ == "__main__":
    unittest.main()

import unittest

from src.me_finder.components.transfer_rate import TransferRate


class TransferRateTests(unittest.TestCase):
    def test_no_estimate_until_samples_span_two_seconds(self):
        rate = TransferRate()
        rate.observe(0.0, 0)
        rate.observe(1.0, 5_000_000)
        self.assertIsNone(rate.bytes_per_second())
        self.assertIsNone(rate.eta_seconds(100_000_000))

    def test_speed_and_time_left_follow_recent_growth(self):
        rate = TransferRate()
        for second in range(0, 6):
            rate.observe(float(second), second * 2_000_000)
        self.assertEqual(rate.bytes_per_second(), 2_000_000)
        self.assertEqual(rate.eta_seconds(30_000_000), 10)

    def test_window_forgets_an_old_fast_start(self):
        rate = TransferRate(window_seconds=10)
        rate.observe(0.0, 0)
        rate.observe(1.0, 50_000_000)
        for second in range(2, 31):
            rate.observe(float(second), 50_000_000 + (second - 1) * 100_000)
        self.assertAlmostEqual(rate.bytes_per_second(), 100_000)

    def test_restarted_download_resets_samples(self):
        rate = TransferRate()
        rate.observe(0.0, 0)
        rate.observe(5.0, 10_000_000)
        rate.observe(6.0, 1_000)
        self.assertIsNone(rate.bytes_per_second())

    def test_stall_reports_zero_speed_and_no_time_left(self):
        rate = TransferRate()
        rate.observe(0.0, 7)
        rate.observe(5.0, 7)
        self.assertEqual(rate.bytes_per_second(), 0)
        self.assertIsNone(rate.eta_seconds(100))


if __name__ == "__main__":
    unittest.main()

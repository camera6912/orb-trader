import unittest

from src.utils.price_utils import round_to_tick


class TestRoundToTick(unittest.TestCase):
    def test_round_down_to_quarter(self):
        self.assertEqual(round_to_tick(6854.62, 0.25, direction="down"), 6854.5)
        self.assertEqual(round_to_tick(6854.75, 0.25, direction="down"), 6854.75)

    def test_round_up_to_quarter(self):
        self.assertEqual(round_to_tick(6854.62, 0.25, direction="up"), 6854.75)
        self.assertEqual(round_to_tick(6854.5, 0.25, direction="up"), 6854.5)


if __name__ == "__main__":
    unittest.main()

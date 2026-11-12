"""Pure refund-calculation tests."""

import unittest

from rental_marketplace.services.cancellation_service import calculate_refund

FLEX = {"policy_type": "Fully Flexible", "free_cancellation_hours": 24,
        "penalty_type": "Percentage", "penalty_value": 0,
        "no_show_penalty": 100}
PARTIAL = {"policy_type": "Partially Refundable",
           "free_cancellation_hours": 48, "penalty_type": "Percentage",
           "penalty_value": 30, "no_show_penalty": 100}
NONREF = {"policy_type": "Non Refundable", "free_cancellation_hours": 0,
          "penalty_type": "Percentage", "penalty_value": 100,
          "no_show_penalty": 100}


class TestRefunds(unittest.TestCase):

    def test_free_window_full_refund(self):
        r = calculate_refund(FLEX, 500, hours_to_pickup=72,
                             daily_equivalent=100, commission_amount=75)
        self.assertEqual(r["eligible_refund"], 500.0)
        self.assertEqual(r["penalty"], 0.0)
        self.assertEqual(r["commission_adjustment"], 75.0)  # fully reversed

    def test_partial_inside_window(self):
        r = calculate_refund(PARTIAL, 500, hours_to_pickup=10,
                             daily_equivalent=100, commission_amount=75)
        self.assertEqual(r["penalty"], 150.0)          # 30% of 500
        self.assertEqual(r["eligible_refund"], 350.0)

    def test_non_refundable(self):
        r = calculate_refund(NONREF, 500, hours_to_pickup=100,
                             daily_equivalent=100, commission_amount=75)
        self.assertEqual(r["eligible_refund"], 0.0)
        self.assertEqual(r["penalty"], 500.0)
        self.assertEqual(r["commission_adjustment"], 0.0)  # fully retained

    def test_first_day_penalty(self):
        pol = dict(PARTIAL, penalty_type="First Day")
        r = calculate_refund(pol, 500, hours_to_pickup=10,
                             daily_equivalent=100, commission_amount=75)
        self.assertEqual(r["penalty"], 100.0)
        self.assertEqual(r["eligible_refund"], 400.0)

    def test_no_show(self):
        r = calculate_refund(FLEX, 500, hours_to_pickup=-1,
                             daily_equivalent=100, commission_amount=75,
                             no_show=True)
        self.assertEqual(r["eligible_refund"], 0.0)

    def test_money_conserved(self):
        for pol, hours in ((FLEX, 72), (PARTIAL, 10), (NONREF, 100)):
            r = calculate_refund(pol, 500, hours, 100, 75)
            retained = r["penalty"]
            self.assertAlmostEqual(r["eligible_refund"] + retained, 500.0, 2)


if __name__ == "__main__":
    unittest.main()

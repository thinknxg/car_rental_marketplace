"""Pure-function pricing tests — no site required.
Run: python3 -m unittest rental_marketplace.tests.test_pricing
"""

import unittest

from rental_marketplace.services.pricing_service import (
    score_rule, select_rule, base_amount_for, compute_extras)

CTX = {"supplier": "SUP-0001", "vehicle_class": "SUV",
       "pickup_location": "MCT", "rental_days": 5,
       "pickup_date": "2026-09-01"}


class TestRulePrecedence(unittest.TestCase):

    def test_specificity_scores(self):
        self.assertEqual(score_rule({"supplier": "SUP-0001",
                                     "vehicle_class": "SUV",
                                     "location": "MCT",
                                     "start_date": "2026-08-01",
                                     "end_date": "2026-12-31"}, CTX), 15)
        self.assertEqual(score_rule({"supplier": "SUP-0001",
                                     "vehicle_class": "SUV",
                                     "location": "MCT"}, CTX), 14)
        self.assertEqual(score_rule({"supplier": "SUP-0001",
                                     "vehicle_class": "SUV"}, CTX), 12)
        self.assertEqual(score_rule({"vehicle_class": "SUV"}, CTX), 4)

    def test_non_match_returns_none(self):
        self.assertIsNone(score_rule({"supplier": "SUP-0002"}, CTX))
        self.assertIsNone(score_rule({"vehicle_class": "Sedan"}, CTX))
        self.assertIsNone(score_rule({"minimum_days": 7}, CTX))
        self.assertIsNone(score_rule({"maximum_days": 3}, CTX))
        self.assertIsNone(score_rule({"start_date": "2026-10-01"}, CTX))

    def test_deterministic_selection(self):
        rules = [
            {"name": "PRL-3", "vehicle_class": "SUV", "priority": 99},
            {"name": "PRL-1", "supplier": "SUP-0001", "vehicle_class": "SUV",
             "location": "MCT", "priority": 0},
            {"name": "PRL-2", "supplier": "SUP-0001", "vehicle_class": "SUV",
             "priority": 50},
        ]
        # specificity beats priority
        self.assertEqual(select_rule(rules, CTX)["name"], "PRL-1")
        # equal specificity → priority, then name
        tie = [{"name": "PRL-B", "vehicle_class": "SUV", "priority": 1},
               {"name": "PRL-A", "vehicle_class": "SUV", "priority": 1}]
        self.assertEqual(select_rule(tie, CTX)["name"], "PRL-A")


class TestAmounts(unittest.TestCase):

    def test_daily_only(self):
        self.assertEqual(float(base_amount_for({"daily_rate": 40}, 5)), 200.0)

    def test_weekly_bundle(self):
        # 10 days = 1 week @ 210 + 3 days @ 40
        amt = base_amount_for({"daily_rate": 40, "weekly_rate": 210}, 10)
        self.assertEqual(float(amt), 330.0)

    def test_monthly_bundle(self):
        # 33 days = 1 month @ 800 + 3 days @ 40
        amt = base_amount_for(
            {"daily_rate": 40, "weekly_rate": 210, "monthly_rate": 800}, 33)
        self.assertEqual(float(amt), 920.0)

    def test_extras_methods(self):
        rows = [
            {"pricing_method": "Per Rental", "price": 20, "quantity": 1},
            {"pricing_method": "Per Day", "price": 5, "quantity": 1},
            {"pricing_method": "Per Unit", "price": 10, "quantity": 2},
            {"pricing_method": "Percentage", "price": 10, "quantity": 1},
        ]
        total, lines = compute_extras(rows, rental_days=4, base_amount=200)
        # 20 + 5*4 + 10*2 + 10% of 200 = 20+20+20+20 = 80
        self.assertEqual(float(total), 80.0)
        self.assertEqual(len(lines), 4)


if __name__ == "__main__":
    unittest.main()

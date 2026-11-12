"""Booking state-machine matrix tests — validates the TRANSITIONS
table without a site (no transitions executed)."""

import unittest

from rental_marketplace.services.booking_service import TRANSITIONS

HAPPY_PATH = ["confirm", "supplier_confirm", "ready", "pickup",
              "activate", "return", "complete"]


class TestTransitions(unittest.TestCase):

    def test_happy_path_is_linear(self):
        state = "Pending Payment"
        for action in HAPPY_PATH:
            spec = TRANSITIONS[action]
            self.assertIn(state, spec["from"],
                          f"{action} not reachable from {state}")
            state = spec["to"]
        self.assertEqual(state, "Completed")

    def test_no_transition_from_terminal_states(self):
        # "dispute" is the one operator-only exception: a completed
        # rental may still be disputed.
        for terminal in ("Completed", "Cancelled", "No Show"):
            for action, spec in TRANSITIONS.items():
                if action == "dispute":
                    continue
                self.assertNotIn(terminal, spec["from"],
                                 f"{action} escapes terminal {terminal}")

    def test_cancel_only_before_pickup(self):
        for src in TRANSITIONS["cancel"]["from"]:
            self.assertNotIn(src, ("Picked Up", "Active Rental", "Returned"))

    def test_every_action_declares_roles(self):
        for action, spec in TRANSITIONS.items():
            self.assertTrue(spec.get("roles"), f"{action} has no role gate")
            self.assertTrue(spec.get("from") and spec.get("to"))


if __name__ == "__main__":
    unittest.main()

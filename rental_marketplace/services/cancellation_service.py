"""CancellationEngine — deterministic, auditable refunds.

calculate_refund() is a pure function (unit-testable). cancel_booking()
performs the full pipeline: eligibility → policy snapshot → refund calc
→ inventory release → commission reversal → refund record → ledger →
notifications → audit → state transition.
"""

import json
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe.utils import now_datetime, get_datetime, time_diff_in_hours

from rental_marketplace.services import audit, notification_service
from rental_marketplace.exceptions import CancellationNotAllowed

TWO = Decimal("0.01")


def q2(x):
    return Decimal(str(x or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


CANCELLABLE = ("Pending Payment", "Confirmed", "Supplier Confirmed",
               "Ready for Pickup")


def calculate_refund(policy, gross_paid, hours_to_pickup, daily_equivalent,
                     commission_amount, no_show=False):
    """Pure refund calculation.
    Returns dict(eligible_refund, penalty, commission_adjustment,
    supplier_adjustment)."""
    gross = q2(gross_paid)
    commission = q2(commission_amount)
    policy_type = (policy or {}).get("policy_type") or "Fully Flexible"
    free_hours = (policy or {}).get("free_cancellation_hours") or 0

    if no_show:
        pct = q2((policy or {}).get("no_show_penalty") or 100)
        penalty = q2(gross * pct / Decimal("100"))
    elif policy_type == "Non Refundable":
        penalty = gross
    elif hours_to_pickup >= free_hours or policy_type == "Fully Flexible" \
            and hours_to_pickup >= free_hours:
        penalty = Decimal("0")
    else:
        ptype = (policy or {}).get("penalty_type") or "Percentage"
        pval = q2((policy or {}).get("penalty_value") or 0)
        if ptype == "Fixed":
            penalty = min(pval, gross)
        elif ptype == "First Day":
            penalty = min(q2(daily_equivalent), gross)
        else:  # Percentage
            penalty = q2(gross * pval / Decimal("100"))

    refund = q2(gross - penalty)
    # Commission follows the money: keep commission proportional to
    # what the marketplace retains; reverse the rest.
    if gross > 0:
        retained_ratio = penalty / gross
    else:
        retained_ratio = Decimal("0")
    commission_kept = q2(commission * retained_ratio)
    commission_adjustment = q2(commission - commission_kept)   # reversed
    supplier_kept = q2(penalty - commission_kept)
    supplier_adjustment = q2((gross - commission) - supplier_kept)  # reduce payable

    return {
        "eligible_refund": float(refund),
        "penalty": float(penalty),
        "commission_adjustment": float(commission_adjustment),
        "supplier_adjustment": float(supplier_adjustment),
        "customer_credit": 0.0,
    }


def cancel_booking(booking_name, actor_user, reason="Customer cancellation",
                   no_show=False):
    from rental_marketplace.services import booking_service

    booking = frappe.get_doc("Booking", booking_name)
    is_operator = bool(set(frappe.get_roles())
                       & booking_service.OPERATOR_ROLES)
    if booking.customer != actor_user and not is_operator:
        frappe.throw("Not your booking.", frappe.PermissionError)
    if booking.booking_status not in CANCELLABLE:
        raise CancellationNotAllowed(
            f"Booking in state {booking.booking_status} cannot be cancelled.")

    policy = json.loads(booking.policy_snapshot or "{}")
    hours_to_pickup = time_diff_in_hours(
        get_datetime(booking.pickup_datetime), now_datetime())
    rental_days = max(
        (get_datetime(booking.dropoff_datetime)
         - get_datetime(booking.pickup_datetime)).days, 1)
    daily_equiv = float(q2(booking.gross_amount) / Decimal(str(rental_days)))

    calc = calculate_refund(
        policy=policy, gross_paid=booking.amount_paid or 0,
        hours_to_pickup=hours_to_pickup, daily_equivalent=daily_equiv,
        commission_amount=booking.commission_amount or 0, no_show=no_show)

    refund = None
    if (booking.amount_paid or 0) > 0:
        refund = frappe.new_doc("Marketplace Refund")
        refund.booking = booking.name
        refund.customer = booking.customer
        refund.status = "Pending"
        refund.reason = "No Show Adjustment" if no_show else "Cancellation"
        refund.gross_amount = booking.amount_paid
        refund.penalty_amount = calc["penalty"]
        refund.refund_amount = calc["eligible_refund"]
        refund.commission_adjustment = calc["commission_adjustment"]
        refund.supplier_adjustment = (calc["supplier_adjustment"]
                                      if booking.get("collect_full_amount") else 0)
        refund.currency = booking.currency
        refund.policy_snapshot = booking.policy_snapshot
        refund.insert(ignore_permissions=True)
        _post_refund_ledger(booking, refund)

    # release vehicle inventory
    if booking.availability_block:
        frappe.db.set_value("Vehicle Availability Block",
                            booking.availability_block, "status", "Released")

    booking_service.transition(
        booking, "no_show" if no_show else "cancel", system=True,
        metadata={"reason": reason, **calc,
                  "refund": refund.name if refund else None})

    if refund:
        refund.db_set("status", "Processed")
        refund.db_set("processed_at", now_datetime())
        booking.db_set("payment_status",
                       "Refunded" if calc["penalty"] == 0
                       else "Partially Refunded")
        notification_service.send("refund_processed", booking.customer,
                                  {"booking": booking.name})
        audit.log("refund_issued", "Marketplace Refund", refund.name,
                  metadata=calc)
    return {"booking": booking.name,
            "refund": refund.name if refund else None, **calc}


def _post_refund_ledger(booking, refund):
    rows = [
        ("Customer Refund", refund.refund_amount),
        ("Commission Reversal", refund.commission_adjustment),
        ("Supplier Adjustment", refund.supplier_adjustment),
    ]
    for entry_type, amount in rows:
        if not amount:
            continue
        e = frappe.new_doc("Marketplace Ledger Entry")
        e.posting_datetime = now_datetime()
        e.entry_type = entry_type
        e.booking = booking.name
        e.supplier = booking.supplier
        e.amount = amount
        e.currency = booking.currency
        e.reference_doctype = "Marketplace Refund"
        e.reference_name = refund.name
        e.insert(ignore_permissions=True)

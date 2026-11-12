"""SettlementService — supplier payouts from historical snapshots.

Settlements are built from booking snapshot amounts and refund
adjustments recorded at the time they happened. Today's commission
rules are never applied to historical periods.
"""

import frappe
from frappe.utils import now_datetime, add_days, getdate

from rental_marketplace.services import audit, notification_service


def generate_for_period(supplier, period_start, period_end):
    """Idempotent per supplier+period — an existing settlement for the
    same window is returned unchanged."""
    existing = frappe.db.get_value(
        "Supplier Settlement",
        {"supplier": supplier, "period_start": period_start,
         "period_end": period_end})
    if existing:
        return frappe.get_doc("Supplier Settlement", existing)

    bookings = frappe.get_all(
        "Booking",
        filters={"supplier": supplier, "booking_status": "Completed", "collect_full_amount": 1,
                 "completed_at": ["between",
                                  [str(period_start), str(period_end) + " 23:59:59"]]},
        fields=["name", "gross_amount", "commission_amount", "tax_amount",
                "supplier_amount", "currency"])
    refund_rows = frappe.get_all(
        "Marketplace Ledger Entry",
        filters={"supplier": supplier, "settled": 0,
                 "entry_type": "Supplier Adjustment"},
        fields=["name", "booking", "amount"])
    if not bookings and not refund_rows:
        return None

    s = frappe.new_doc("Supplier Settlement")
    s.supplier = supplier
    s.period_start = period_start
    s.period_end = period_end
    s.status = "Draft"
    gross = commission = refunds = tax = 0.0
    for b in bookings:
        s.append("lines", {"booking": b.name, "line_type": "Booking",
                           "gross": b.gross_amount,
                           "commission": b.commission_amount,
                           "net": b.supplier_amount})
        gross += b.gross_amount or 0
        commission += b.commission_amount or 0
        tax += b.tax_amount or 0
        s.currency = s.currency or b.currency
    for r in refund_rows:
        s.append("lines", {"booking": r.booking, "line_type": "Refund",
                           "gross": 0, "commission": 0, "net": -r.amount})
        refunds += r.amount or 0
    s.gross_booking_amount = round(gross, 2)
    s.commission = round(commission, 2)
    s.refunds = round(refunds, 2)
    s.adjustments = 0
    s.tax = round(tax, 2)
    s.insert(ignore_permissions=True)
    s.db_set("status", "Calculated")

    for b in bookings:
        for mle in frappe.get_all("Marketplace Ledger Entry",
                                  filters={"booking": b.name, "settled": 0},
                                  pluck="name"):
            frappe.db.set_value("Marketplace Ledger Entry", mle,
                                {"settled": 1, "settlement": s.name})
    for r in refund_rows:
        frappe.db.set_value("Marketplace Ledger Entry", r.name,
                            {"settled": 1, "settlement": s.name})

    audit.log("settlement_generated", "Supplier Settlement", s.name,
              metadata={"supplier": supplier, "net": s.net_payable})
    return s


def mark_paid(settlement_name, payment_reference):
    s = frappe.get_doc("Supplier Settlement", settlement_name)
    s.payment_reference = payment_reference
    s.status = "Paid"
    s.paid_at = now_datetime()
    s.save(ignore_permissions=False)  # Finance-role gate in controller
    audit.log("settlement_paid", "Supplier Settlement", s.name,
              metadata={"reference": payment_reference})
    for pu in frappe.get_all("Supplier Portal User",
                             filters={"parent": s.supplier}, fields=["user"]):
        notification_service.send("settlement_paid", pu.user,
                                  {"settlement": s.name})
    return s

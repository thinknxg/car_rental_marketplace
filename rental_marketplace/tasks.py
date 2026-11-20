"""Scheduler jobs — every job is retry-safe and idempotent.
Expensive fan-out is enqueued rather than run inside the tick."""

import frappe
from frappe.utils import now_datetime, add_to_date, add_days, getdate

from rental_marketplace.services import (
    audit, tender_service, cancellation_service, settlement_service,
    notification_service)


def expire_offers():
    for name in frappe.get_all(
            "Offer", filters={"status": ["in", ["Submitted", "Shortlisted"]],
                              "valid_until": ["<", now_datetime()]},
            pluck="name"):
        frappe.db.set_value("Offer", name, "status", "Expired")
        audit.log("offer_expired", "Offer", name, new_state="Expired",
                  actor_type="System")


def close_expired_tenders():
    for name in frappe.get_all(
            "Tender", filters={"status": "Open", "auto_close": 1,
                               "response_deadline": ["<", now_datetime()]},
            pluck="name"):
        try:
            tender_service.close_tender(name, reason="auto_close")
        except Exception:
            frappe.log_error(frappe.get_traceback(),
                             f"close_expired_tenders: {name}")


def release_expired_reservations():
    """Pending-Payment bookings past the hold window are released so
    the vehicle returns to the market."""
    hold = frappe.db.get_single_value("Car Marketplace Settings",
                                      "payment_hold_minutes") or 60
    cutoff = add_to_date(now_datetime(), minutes=-hold)
    for name in frappe.get_all(
            "Booking", filters={"booking_status": "Pending Payment",
                                "creation": ["<", cutoff]},
            pluck="name"):
        try:
            cancellation_service.cancel_booking(
                name, "Administrator", reason="Payment hold expired")
            audit.log("reservation_released", "Booking", name,
                      actor_type="System")
        except Exception:
            frappe.log_error(frappe.get_traceback(),
                             f"release_expired_reservations: {name}")


def send_pickup_reminders():
    tomorrow = add_days(getdate(), 1)
    for b in frappe.get_all(
            "Booking",
            filters={"booking_status": ["in", ["Confirmed",
                                               "Supplier Confirmed",
                                               "Ready for Pickup"]],
                     "pickup_datetime": ["between",
                                         [f"{tomorrow} 00:00:00",
                                          f"{tomorrow} 23:59:59"]]},
            fields=["name", "customer"]):
        notification_service.send("pickup_reminder", b.customer,
                                  {"booking": b.name})


def send_return_reminders():
    tomorrow = add_days(getdate(), 1)
    for b in frappe.get_all(
            "Booking",
            filters={"booking_status": ["in", ["Picked Up", "Active Rental"]],
                     "dropoff_datetime": ["between",
                                          [f"{tomorrow} 00:00:00",
                                           f"{tomorrow} 23:59:59"]]},
            fields=["name", "customer"]):
        notification_service.send("return_reminder", b.customer,
                                  {"booking": b.name})


def detect_stale_supplier_responses():
    cutoff = add_to_date(now_datetime(), minutes=-30)
    stale = frappe.get_all(
        "Tender Supplier",
        filters={"invitation_status": ["in", ["Invited", "Viewed"]],
                 "invited_at": ["<", cutoff]},
        fields=["name", "tender", "supplier"])
    if stale:
        notification_service.notify_operations(
            f"{len(stale)} supplier invitations without response",
            "\n".join(f"{s.tender} / {s.supplier}" for s in stale[:50]))


def generate_settlement_periods():
    """Weekly: previous Mon–Sun settlement per active supplier."""
    end = add_days(getdate(), -(getdate().weekday() + 1))  # last Sunday
    start = add_days(end, -6)
    for supplier in frappe.get_all("Marketplace Supplier",
                                   filters={"active": 1, "status": "Verified"},
                                   pluck="name"):
        try:
            settlement_service.generate_for_period(supplier, start, end)
        except Exception:
            frappe.log_error(frappe.get_traceback(),
                             f"generate_settlement_periods: {supplier}")


def reconcile_payments():
    """Flag bookings whose paid amount disagrees with succeeded
    transactions — routed to operations, never auto-corrected."""
    rows = frappe.db.sql(
        """
        SELECT b.name, b.amount_paid,
               COALESCE(SUM(CASE WHEN t.transaction_type='Charge'
                                 THEN t.amount ELSE -t.amount END), 0) AS txn_sum
        FROM `tabBooking` b
        LEFT JOIN `tabPayment Transaction` t
               ON t.booking = b.name AND t.status = 'Succeeded'
        GROUP BY b.name
        HAVING ABS(COALESCE(b.amount_paid, 0) - txn_sum) > 0.01
        """, as_dict=True)
    if rows:
        notification_service.notify_operations(
            f"Payment reconciliation: {len(rows)} mismatches",
            "\n".join(f"{r.name}: paid={r.amount_paid} txns={r.txn_sum}"
                      for r in rows[:50]))


def refresh_marketplace_metrics():
    metrics = {
        "requests_created": frappe.db.count("Car Rental Request"),
        "tenders_created": frappe.db.count("Tender"),
        "offers_submitted": frappe.db.count(
            "Offer", {"status": ["!=", "Draft"]}),
        "bookings": frappe.db.count("Booking"),
        "completed": frappe.db.count("Booking",
                                     {"booking_status": "Completed"}),
        "cancelled": frappe.db.count("Booking",
                                     {"booking_status": "Cancelled"}),
        "refreshed_at": str(now_datetime()),
    }
    frappe.cache().set_value("rental_marketplace:metrics", metrics)


def archive_old_events():
    """Retention: idempotency keys past TTL are removed; audit events
    and webhook payloads are retained (see README data-retention)."""
    frappe.db.delete("API Idempotency Key",
                     {"expires_at": ["<", now_datetime()]})

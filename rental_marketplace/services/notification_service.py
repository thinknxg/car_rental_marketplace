"""Notification abstraction.

notification_service.send(event, recipient, context)

Channels: in-app (Notification Log), email (best effort), realtime.
SMS/push are Release-2 — the abstraction keeps call sites stable.
Sends never raise into business flows.
"""

import frappe

SUBJECTS = {
    "rental_request_submitted": "Your rental request was submitted",
    "tender_opened":            "Your request is out for offers",
    "supplier_invited":         "New tender available: {tender}",
    "offer_received":           "New offer received for your request",
    "offer_expiring":           "An offer on your request expires soon",
    "offer_accepted":           "Your offer was accepted",
    "booking_confirmed":        "Booking confirmed: {booking}",
    "payment_received":         "Payment received for {booking}",
    "supplier_confirmation_required": "Please confirm booking {booking}",
    "pickup_reminder":          "Pickup reminder: {booking}",
    "return_reminder":          "Return reminder: {booking}",
    "booking_cancelled":        "Booking cancelled: {booking}",
    "refund_processed":         "Your refund has been processed",
    "settlement_paid":          "Settlement paid: {settlement}",
}


def send(event, recipient, context=None):
    context = context or {}
    subject = SUBJECTS.get(event, event).format(**{
        k: str(v) for k, v in context.items()})
    try:
        frappe.get_doc({
            "doctype": "Notification Log",
            "for_user": recipient,
            "type": "Alert",
            "subject": subject,
            "email_content": context.get("message") or subject,
            "document_type": context.get("doctype"),
            "document_name": context.get("docname"),
        }).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"notify in-app: {event}")

    try:
        if recipient and "@" in (recipient or "") and recipient != "Guest":
            frappe.sendmail(recipients=[recipient], subject=subject,
                            message=context.get("message") or subject,
                            delayed=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"notify email: {event}")

    try:
        frappe.publish_realtime(f"car_marketplace:{event}",
                                message=context, user=recipient)
    except Exception:
        pass


def notify_operations(subject, message=""):
    ops = frappe.db.get_single_value("Car Marketplace Settings", "operations_email")
    if not ops:
        return
    try:
        frappe.sendmail(recipients=[ops], subject=subject,
                        message=message or subject, delayed=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify_operations")

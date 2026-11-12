import frappe


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/portal"
        raise frappe.Redirect
    user = frappe.session.user
    context.no_cache = 1
    context.requests = frappe.get_all(
        "Rental Request", filters={"customer": user},
        fields=["name", "status", "pickup_location", "pickup_datetime",
                "dropoff_datetime", "vehicle_class"],
        order_by="creation desc", limit_page_length=8)
    for r in context.requests:
        r.tender = frappe.db.get_value(
            "Tender", {"rental_request": r.name}, "name")
        r.offer_count = frappe.db.count("Offer", {
            "tender": r.tender,
            "status": ["in", ["Submitted", "Shortlisted"]]}) if r.tender else 0
    context.bookings = frappe.get_all(
        "Booking", filters={"customer": user},
        fields=["name", "booking_status", "payment_status", "gross_amount",
                "amount_due", "currency", "pickup_datetime",
                "pickup_location"],
        order_by="creation desc", limit_page_length=8)
    context.action_needed = [b for b in context.bookings
                             if b.booking_status == "Pending Payment"]
    context.profile = frappe.db.get_value(
        "Customer Profile", {"user": user},
        ["first_name", "last_name", "verification_status"], as_dict=True)
    return context

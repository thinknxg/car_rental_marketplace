import frappe


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/partner"
        raise frappe.Redirect
    partner = frappe.db.get_value(
        "Marketplace Partner", {"user": frappe.session.user},
        ["name", "partner_name", "partner_code", "status", "partner_type",
         "commission_share_percentage"], as_dict=True)
    if not partner:
        frappe.throw("No partner account linked to your user.",
                     frappe.PermissionError)
    context.no_cache = 1
    context.partner = partner
    context.requests_attributed = frappe.db.count(
        "Rental Request", {"partner": partner.name})
    context.bookings = frappe.get_all(
        "Booking", filters={"partner": partner.name},
        fields=["name", "booking_status", "gross_amount",
                "partner_commission", "currency", "pickup_datetime"],
        order_by="creation desc", limit_page_length=40)
    context.earned = round(sum(
        b.partner_commission or 0 for b in context.bookings
        if b.booking_status == "Completed"), 2)
    context.pipeline = round(sum(
        b.partner_commission or 0 for b in context.bookings
        if b.booking_status not in ("Completed", "Cancelled", "No Show")), 2)
    context.paid_out = round(sum(
        e.amount or 0 for e in frappe.get_all(
            "Marketplace Ledger Entry",
            filters={"partner": partner.name,
                     "entry_type": "Partner Commission", "settled": 1},
            fields=["amount"])), 2)
    return context

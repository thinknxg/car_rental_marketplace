import frappe
from rental_marketplace.permissions import get_user_supplier


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login"
        raise frappe.Redirect
    supplier = get_user_supplier(frappe.session.user)
    if not supplier:
        frappe.throw("No supplier account linked to your user.",
                     frappe.PermissionError)
    context.no_cache = 1
    context.supplier = supplier
    context.supplier_name = frappe.db.get_value(
        "Marketplace Supplier", supplier, "supplier_name")
    rows = frappe.get_all(
        "Tender Supplier",
        filters={"supplier": supplier,
                 "invitation_status": ["in", ["Invited", "Viewed",
                                              "Responded"]]},
        fields=["name", "tender", "invitation_status", "invited_at"],
        order_by="invited_at desc", limit_page_length=50)
    tenders = []
    for r in rows:
        t = frappe.db.get_value(
            "Tender", r.tender,
            ["name", "status", "pickup_location", "dropoff_location",
             "pickup_datetime", "dropoff_datetime", "vehicle_class",
             "response_deadline", "currency"], as_dict=True)
        if t:
            t["invitation_status"] = r.invitation_status
            t["booking"] = None
            if t.status == "Awarded":
                offer_name = frappe.db.get_value(
                    "Offer", {"tender": t.name, "supplier": supplier,
                              "status": "Accepted"}, "name")
                if offer_name:
                    t["booking"] = frappe.db.get_value(
                        "Booking", {"accepted_offer": offer_name}, "name")
            tenders.append(t)
    context.tenders = tenders
    context.vehicles = frappe.get_all(
        "Marketplace Vehicle", filters={"supplier": supplier, "active": 1},
        fields=["name", "display_name", "vehicle_class"])
    context.policies = frappe.get_all(
        "Cancellation Policy",
        filters={"active": 1},
        or_filters=[["supplier", "=", supplier], ["supplier", "is", "not set"]],
        fields=["name", "policy_name"])
    return context

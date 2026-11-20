import frappe
from frappe.utils import now_datetime
from rental_marketplace.permissions import get_user_supplier


def _guard():
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login"
        raise frappe.Redirect
    supplier = get_user_supplier(frappe.session.user)
    if not supplier:
        frappe.throw("No supplier account linked to your user.",
                     frappe.PermissionError)
    return supplier


def get_context(context):
    supplier = _guard()
    context.no_cache = 1
    context.supplier = supplier
    sup = frappe.db.get_value(
        "Marketplace Supplier", supplier,
        ["supplier_name", "status", "rating", "reliability_score"],
        as_dict=True)
    context.sup = sup

    context.kpi = {
        "open_tenders": frappe.db.count("Tender Supplier", {
            "supplier": supplier,
            "invitation_status": ["in", ["Invited", "Viewed"]]}),
        "live_offers": frappe.db.count("Offer", {
            "supplier": supplier,
            "status": ["in", ["Submitted", "Shortlisted"]]}),
        "upcoming": frappe.db.count("Booking", {
            "supplier": supplier,
            "booking_status": ["in", ["Confirmed", "Supplier Confirmed",
                                      "Ready for Pickup"]]}),
        "active": frappe.db.count("Booking", {
            "supplier": supplier,
            "booking_status": ["in", ["Picked Up", "Active Rental"]]}),
        "completed": frappe.db.count("Booking", {
            "supplier": supplier, "booking_status": "Completed"}),
        "cancelled": frappe.db.count("Booking", {
            "supplier": supplier, "booking_status": "Cancelled"}),
        "vehicles": frappe.db.count("Marketplace Vehicle", {
            "supplier": supplier, "active": 1}),
    }
    total = context.kpi["completed"] + context.kpi["cancelled"]
    context.kpi["cancellation_rate"] = round(
        100.0 * context.kpi["cancelled"] / total, 1) if total else 0.0

    fin = frappe.db.sql(
        """SELECT COALESCE(SUM(gross_amount),0) g,
                  COALESCE(SUM(commission_amount),0) c,
                  COALESCE(SUM(supplier_amount),0) n
           FROM `tabBooking`
           WHERE supplier=%s AND booking_status='Completed'""",
        supplier, as_dict=True)[0]
    pending = frappe.db.sql(
        """SELECT COALESCE(SUM(net_payable),0) FROM `tabSupplier Settlement`
           WHERE supplier=%s AND status IN ('Calculated','Approved',
                                            'Processing')""", supplier)[0][0]
    context.fin = {"gross": fin.g, "commission": fin.c, "net": fin.n,
                   "pending_settlement": pending or 0}
    return context

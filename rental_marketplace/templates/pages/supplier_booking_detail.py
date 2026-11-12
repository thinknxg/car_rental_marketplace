import frappe
from rental_marketplace.templates.pages.supplier_home import _guard


def get_context(context):
    supplier = _guard()
    context.no_cache = 1
    booking_id = frappe.form_dict.get("booking_id") or ""
    doc = frappe.get_doc("Booking", booking_id)
    if doc.supplier != supplier:
        frappe.throw("Not your booking.", frappe.PermissionError)
    context.booking = doc.as_dict(convert_dates_to_str=True)
    context.customer_name = frappe.db.get_value(
        "Customer Profile", {"user": doc.customer},
        ["first_name", "last_name", "phone"], as_dict=True) or {}
    context.customer_email = doc.customer
    return context

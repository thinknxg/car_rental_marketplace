import frappe
from rental_marketplace.templates.pages.supplier_home import _guard


def get_context(context):
    supplier = _guard()
    context.no_cache = 1
    vehicle_id = frappe.form_dict.get("vehicle_id") or ""
    v = frappe.get_doc("Marketplace Vehicle", vehicle_id)
    if v.supplier != supplier:
        frappe.throw("Not your vehicle.", frappe.PermissionError)
    context.vehicle = v.as_dict()
    context.active_blocks = frappe.get_all(
        "Vehicle Availability Block",
        filters={"vehicle": vehicle_id, "status": "Active"},
        fields=["name", "block_type", "start_datetime", "end_datetime",
                "booking"], order_by="start_datetime")
    context.bookings = frappe.get_all(
        "Booking", filters={"vehicle": vehicle_id, "supplier": supplier},
        fields=["name", "booking_status", "payment_status", "customer",
                "pickup_datetime", "dropoff_datetime", "supplier_amount",
                "currency"], order_by="pickup_datetime desc",
        limit_page_length=25)
    return context

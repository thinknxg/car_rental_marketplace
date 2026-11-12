import frappe
from rental_marketplace.templates.pages.supplier_home import _guard

ACTIONS = {
    "Confirmed": ("supplier_confirm", "Confirm booking"),
    "Supplier Confirmed": ("ready", "Mark ready"),
    "Ready for Pickup": ("pickup", "Mark picked up"),
    "Picked Up": ("activate", "Start rental"),
    "Active Rental": ("return", "Mark returned"),
    "Returned": ("complete", "Complete"),
}


def get_context(context):
    supplier = _guard()
    context.no_cache = 1
    context.bookings = frappe.get_all(
        "Booking", filters={"supplier": supplier},
        fields=["name", "booking_status", "payment_status", "vehicle",
                "pickup_location", "pickup_datetime", "dropoff_datetime",
                "supplier_amount", "currency", "customer"],
        order_by="pickup_datetime desc", limit_page_length=40)
    for b in context.bookings:
        b.action = ACTIONS.get(b.booking_status)
    return context

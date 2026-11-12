import frappe
from rental_marketplace.templates.pages.supplier_home import _guard


def get_context(context):
    supplier = _guard()
    context.no_cache = 1
    context.supplier = supplier
    context.vehicles = frappe.get_all(
        "Marketplace Vehicle", filters={"supplier": supplier},
        fields=["name", "display_name", "vehicle_class", "make", "model",
                "model_year", "transmission", "fuel_type", "seats",
                "active", "home_location", "primary_photo",
                "daily_rate", "weekly_rate", "monthly_rate"],
        order_by="vehicle_class, display_name")
    for v in context.vehicles:
        v.active_blocks = frappe.db.count("Vehicle Availability Block", {
            "vehicle": v.name, "status": "Active"})
    context.blocks = frappe.get_all(
        "Vehicle Availability Block",
        filters={"supplier": supplier, "status": "Active"},
        fields=["name", "vehicle", "block_type", "start_datetime",
                "end_datetime", "booking"],
        order_by="start_datetime", limit_page_length=25)
    context.vehicle_classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1}, fields=["name"])
    context.locations = frappe.get_all(
        "Marketplace Location", filters={"active": 1},
        fields=["name", "location_name"])
    return context

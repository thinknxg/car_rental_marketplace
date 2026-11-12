import frappe


def get_context(context):
    context.no_cache = 1
    context.full_width = True
    context.vehicle_classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1},
        fields=["name", "class_name"], order_by="sort_order")
    context.locations = frappe.get_all(
        "Marketplace Location", filters={"active": 1},
        fields=["name", "location_name", "location_type"],
        order_by="location_name")
    return context

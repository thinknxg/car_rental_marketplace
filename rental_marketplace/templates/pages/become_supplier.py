import frappe


def get_context(context):
    context.no_cache = 1
    context.locations = frappe.get_all(
        "Marketplace Location",
        filters={"active": 1},
        fields=["name", "location_name", "location_type", "city", "country"],
        order_by="location_type, location_name",
    )
    return context

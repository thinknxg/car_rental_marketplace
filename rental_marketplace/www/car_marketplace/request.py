import frappe


def get_context(context):
    context.no_cache = 1
    context.full_width = True
    context.prefill_vehicle_class = frappe.form_dict.get("vehicle_class") or ""
    context.prefill_make = frappe.form_dict.get("preferred_make") or ""
    context.prefill_model = frappe.form_dict.get("preferred_model") or ""
    context.locations = frappe.get_all(
        "Marketplace Location", filters={"active": 1},
        fields=["name", "location_name", "location_type"],
        order_by="location_name")
    context.vehicle_classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1},
        fields=["name", "class_name"], order_by="sort_order")
    return context

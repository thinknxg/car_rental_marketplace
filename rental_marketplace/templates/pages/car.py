import frappe


def get_context(context):
    context.no_cache = 1
    context.full_width = True
    vehicle_id = frappe.form_dict.get("vehicle")
    if not vehicle_id:
        frappe.throw("No vehicle specified.", frappe.DoesNotExistError)
    vehicles = frappe.get_all(
        "Marketplace Vehicle",
        filters={"name": vehicle_id, "active": 1},
        fields=["name", "display_name", "make", "model", "model_year",
                "vehicle_class", "transmission", "fuel_type", "seats",
                "primary_photo", "supplier", "daily_rate", "weekly_rate",
                "monthly_rate"])
    if not vehicles:
        frappe.throw("Vehicle not found.", frappe.DoesNotExistError)
    context.vehicle = vehicles[0]
    context.locations = frappe.get_all(
        "Marketplace Location", filters={"active": 1},
        fields=["name", "location_name", "location_type"],
        order_by="location_name")
    context.vehicle_classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1},
        fields=["name", "class_name"], order_by="sort_order")
    context.related_vehicles = frappe.get_all(
        "Marketplace Vehicle",
        filters={"active": 1, "name": ["!=", vehicle_id],
                 "vehicle_class": ["in", ["Luxury", "SUV", "Convertible", "Electric"]]},
        fields=["name", "display_name", "make", "model", "vehicle_class",
                "primary_photo", "daily_rate"],
        order_by="daily_rate desc",
        limit_page_length=4)
    return context

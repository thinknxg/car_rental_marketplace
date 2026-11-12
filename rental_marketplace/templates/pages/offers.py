import frappe


def get_context(context):
    context.no_cache = 1
    context.full_width = True
    context.locations = frappe.get_all(
        "Marketplace Location", filters={"active": 1},
        fields=["name", "location_name", "location_type"],
        order_by="location_name")
    context.vehicle_classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1},
        fields=["name", "class_name"], order_by="sort_order")
    context.tender_id = frappe.form_dict.get("tender_id") or ""
    context.tender = None
    context.pickup_location_name = ""
    context.dropoff_location_name = ""
    context.matched_supplier_count = 0
    if context.tender_id:
        tender = frappe.db.get_value(
            "Tender", context.tender_id,
            ["pickup_location", "dropoff_location", "pickup_datetime",
             "dropoff_datetime", "vehicle_class"], as_dict=True)
        if tender:
            context.tender = tender
            context.pickup_location_name = frappe.db.get_value(
                "Marketplace Location", tender.pickup_location,
                "location_name") or tender.pickup_location
            context.dropoff_location_name = frappe.db.get_value(
                "Marketplace Location", tender.dropoff_location,
                "location_name") or tender.dropoff_location
            context.matched_supplier_count = frappe.db.count(
                "Tender Supplier", {"tender": context.tender_id})
    return context

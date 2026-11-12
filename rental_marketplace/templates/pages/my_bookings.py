import frappe


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login"
        raise frappe.Redirect
    context.no_cache = 1
    context.full_width = True
    context.locations = frappe.get_all(
        "Marketplace Location", filters={"active": 1},
        fields=["name", "location_name", "location_type"],
        order_by="location_name")
    context.vehicle_classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1},
        fields=["name", "class_name"], order_by="sort_order")

    booking_filters = {"customer": frappe.session.user}
    only_new = frappe.form_dict.get("new")
    if only_new:
        booking_filters["name"] = only_new

    bookings = frappe.get_all(
        "Booking", filters=booking_filters,
        fields=["name", "booking_status", "payment_status", "vehicle",
                "pickup_location", "pickup_datetime", "dropoff_datetime",
                "gross_amount", "amount_due", "commission_amount",
                "tax_amount", "supplier_amount", "deposit_amount", "currency"],
        order_by="creation desc")

    vehicle_ids = list({b.vehicle for b in bookings if b.vehicle})
    vehicles = {}
    if vehicle_ids:
        for v in frappe.get_all(
            "Marketplace Vehicle", filters={"name": ["in", vehicle_ids]},
            fields=["name", "display_name", "make", "model", "model_year",
                    "primary_photo", "transmission", "fuel_type", "seats"]):
            vehicles[v.name] = v

    for b in bookings:
        b["vehicle_detail"] = vehicles.get(b.vehicle)

    context.bookings = bookings
    return context

import frappe

ROLES = ["Car Marketplace Admin", "Car Marketplace Operations", "Car Marketplace Finance",
         "Car Marketplace Support", "Supplier Admin", "Supplier Manager",
         "Supplier Agent", "Supplier Finance", "Car Marketplace Customer"]

VEHICLE_CLASSES = [
    ("Economy", 1), ("Compact", 2), ("Sedan", 3), ("SUV", 4),
    ("Luxury", 5), ("Van", 6), ("Convertible", 7), ("Electric", 8)]


def ensure_roles():
    for role in ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "role_name": role,
                            "desk_access": 0 if role == "Car Marketplace Customer"
                            else 1}).insert(ignore_permissions=True)


def after_install():
    ensure_roles()

    for name, order in VEHICLE_CLASSES:
        if not frappe.db.exists("Vehicle Class", name):
            frappe.get_doc({"doctype": "Vehicle Class", "class_name": name,
                            "sort_order": order,
                            "active": 1}).insert(ignore_permissions=True)

    ensure_custom_fields()

    settings = frappe.get_single("Car Marketplace Settings")
    if not settings.default_currency:
        settings.default_currency = "OMR"
    settings.save(ignore_permissions=True)
    frappe.db.commit()


def ensure_custom_fields():
    """These Booking fields used to exist only as UI-made Custom Fields. Creating
    them from code makes fresh installs (Frappe Cloud, new benches) work."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({"Booking": [
        dict(fieldname="collect_full_amount", label="Collect Full Amount (platform)",
             fieldtype="Check", insert_after="amount_due", default="0"),
        dict(fieldname="sales_invoice", label="Sales Invoice", fieldtype="Link",
             options="Sales Invoice", insert_after="collect_full_amount", read_only=1),
    ]}, update=True)

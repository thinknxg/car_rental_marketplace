import frappe


def get_context(context):
    context.no_cache = 1
    context.full_width = True
    context.booking_id = frappe.form_dict.get("id") or ""
    return context

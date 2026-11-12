"""Server-side ownership checks — marketplace isolation is enforced
here in addition to standard role/DocType permissions."""

import frappe

OPERATOR_ROLES = {"Marketplace Admin", "Marketplace Operations",
                  "Marketplace Finance", "Marketplace Support",
                  "System Manager"}


def is_operator(user):
    return bool(OPERATOR_ROLES & set(frappe.get_roles(user)))


def get_user_supplier(user=None):
    user = user or frappe.session.user
    return frappe.db.get_value("Supplier Portal User", {"user": user},
                               "parent")


# ── permission_query_conditions (list-level row filters) ───────────────────────

def rental_request_query(user):
    if is_operator(user):
        return ""
    return f"`tabRental Request`.customer = {frappe.db.escape(user)}"


def tender_supplier_query(user):
    if is_operator(user):
        return ""
    supplier = get_user_supplier(user)
    if supplier:
        return f"`tabTender Supplier`.supplier = {frappe.db.escape(supplier)}"
    return "1=0"


def offer_query(user):
    if is_operator(user):
        return ""
    supplier = get_user_supplier(user)
    if supplier:
        return f"`tabOffer`.supplier = {frappe.db.escape(supplier)}"
    return "1=0"  # customers consume normalized cards via the API


def booking_query(user):
    if is_operator(user):
        return ""
    supplier = get_user_supplier(user)
    clauses = [f"`tabBooking`.customer = {frappe.db.escape(user)}"]
    if supplier:
        clauses.append(f"`tabBooking`.supplier = {frappe.db.escape(supplier)}")
    return "(" + " OR ".join(clauses) + ")"


def supplier_scoped_query(user, doctype=None):
    if is_operator(user):
        return ""
    supplier = get_user_supplier(user)
    if not supplier:
        return "1=0"
    dt = frappe.local.form_dict.get("doctype") or doctype
    table = f"`tab{dt}`" if dt else ""
    if not table:
        return "1=0"
    return f"{table}.supplier = {frappe.db.escape(supplier)}"


def customer_scoped_query(user, doctype=None):
    if is_operator(user):
        return ""
    dt = frappe.local.form_dict.get("doctype") or doctype
    if not dt:
        return "1=0"
    return f"`tab{dt}`.customer = {frappe.db.escape(user)}"


# ── has_permission (object-level) ──────────────────────────────────────────────

def offer_has_permission(doc, ptype, user):
    if is_operator(user):
        return True
    supplier = get_user_supplier(user)
    if supplier and doc.supplier == supplier:
        # suppliers cannot modify accepted/terminal offers
        if ptype in ("write", "delete") and doc.status in (
                "Accepted", "Rejected", "Superseded", "Expired", "Withdrawn"):
            return False
        return True
    return False


def booking_has_permission(doc, ptype, user):
    if is_operator(user):
        return True
    if doc.customer == user:
        return ptype == "read"
    supplier = get_user_supplier(user)
    if supplier and doc.supplier == supplier:
        return ptype == "read"
    return False

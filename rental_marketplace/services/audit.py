"""Marketplace audit trail. Every commercially important transition
calls audit.log(). Writes are best-effort but errors are logged —
an audit failure must be visible to operators without breaking flows.
"""

import json
import frappe
from frappe.utils import now_datetime


def _actor_type():
    roles = set(frappe.get_roles())
    if frappe.session.user in ("Administrator",) or "Car Marketplace Admin" in roles \
            or "Car Marketplace Operations" in roles:
        return "Operator"
    if roles & {"Supplier Admin", "Supplier Manager", "Supplier Agent",
                "Supplier Finance"}:
        return "Supplier"
    if frappe.session.user == "Guest":
        return "System"
    return "Customer"


def log(event_type, entity_type=None, entity_name=None,
        old_state=None, new_state=None, metadata=None, actor_type=None):
    try:
        doc = frappe.new_doc("Car Marketplace Audit Event")
        doc.created_at = now_datetime()
        doc.event_type = event_type
        doc.actor = frappe.session.user
        doc.actor_type = actor_type or _actor_type()
        doc.entity_type = entity_type
        doc.entity_name = entity_name
        doc.old_state = old_state
        doc.new_state = new_state
        doc.metadata = json.dumps(metadata or {}, default=str)
        try:
            doc.request_id = frappe.local.request_id or ""
            doc.ip_address = frappe.local.request_ip or ""
            doc.user_agent = (frappe.local.request.headers.get("User-Agent")
                              or "")[:200]
        except Exception:
            pass
        doc.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Audit write failed: {event_type}")

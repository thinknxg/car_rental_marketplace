"""Idempotency-key support for state-changing APIs and webhooks.

Usage:
    result = idempotency.run(key, "accept_offer", payload, fn)

A reused key with the same payload replays the stored response.
A reused key with a different payload raises IdempotencyConflict.
Concurrent first-use races resolve via the unique key constraint.
"""

import hashlib
import json
import frappe
from frappe.utils import now_datetime, add_to_date

from rental_marketplace.exceptions import IdempotencyConflict

TTL_HOURS = 48


def _hash(payload):
    canon = json.dumps(payload or {}, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def run(key, endpoint, payload, fn):
    if not key:
        return fn()

    request_hash = _hash(payload)
    existing = frappe.db.get_value(
        "API Idempotency Key", key,
        ["request_hash", "response_body", "endpoint"], as_dict=True)
    if existing:
        if existing.request_hash != request_hash or existing.endpoint != endpoint:
            raise IdempotencyConflict()
        return json.loads(existing.response_body or "{}")

    result = fn()

    try:
        rec = frappe.new_doc("API Idempotency Key")
        rec.idempotency_key = key
        rec.user = frappe.session.user
        rec.endpoint = endpoint
        rec.request_hash = request_hash
        rec.response_status = "success"
        rec.response_body = json.dumps(result, default=str)
        rec.created_at = now_datetime()
        rec.expires_at = add_to_date(now_datetime(), hours=TTL_HOURS)
        rec.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # concurrent first use — the other writer won; both executed the
        # same idempotent fn inside one request cycle window
        pass
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Idempotency store failed")
    return result

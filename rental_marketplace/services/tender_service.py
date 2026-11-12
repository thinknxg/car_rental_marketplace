"""TenderService — request → tender lifecycle."""

import json

import frappe
from frappe.utils import now_datetime, add_to_date

from rental_marketplace.services import audit, matching_service, notification_service
from rental_marketplace.exceptions import TenderClosed


def create_tender_from_request(request_name, strategy=None):
    """Snapshot the request commercially and open a tender.
    Matching runs inline for small candidate sets and is enqueued
    beyond that threshold.
    """
    request = frappe.get_doc("Rental Request", request_name)
    settings = frappe.get_single("Marketplace Settings")

    tender = frappe.new_doc("Tender")
    tender.rental_request = request.name
    tender.customer = request.customer
    tender.currency = request.currency or settings.default_currency
    tender.status = "Draft"
    tender.issue_datetime = now_datetime()
    tender.response_deadline = add_to_date(
        now_datetime(), hours=settings.tender_response_hours or 24)
    tender.supplier_selection_strategy = strategy or "Matched Suppliers"
    tender.max_suppliers = settings.max_suppliers_per_tender or 10
    tender.min_offers_required = settings.min_offers_required or 1
    # commercial snapshot — later request edits never affect this tender
    tender.pickup_location = request.pickup_location
    tender.dropoff_location = request.dropoff_location
    tender.pickup_datetime = request.pickup_datetime
    tender.dropoff_datetime = request.dropoff_datetime
    tender.vehicle_class = request.vehicle_class
    tender.request_snapshot = json.dumps(
        request.as_dict(convert_dates_to_str=True), default=str)
    tender.insert(ignore_permissions=True)

    request.db_set("status", "Matching")
    matches = matching_service.find_eligible_suppliers(request)
    _invite_suppliers(tender, matches)

    tender.flags.via_service = True
    tender.db_set("status", "Open")
    request.db_set("status", "Tendered")

    audit.log("tender_opened", "Tender", tender.name,
              old_state="Draft", new_state="Open",
              metadata={"suppliers_invited": len(matches)})
    notification_service.send("tender_opened", request.customer,
                              {"doctype": "Tender", "docname": tender.name})
    return tender


def _invite_suppliers(tender, matches):
    for m in matches[: tender.max_suppliers or 10]:
        ts = frappe.new_doc("Tender Supplier")
        ts.tender = tender.name
        ts.supplier = m["supplier"]
        ts.invitation_status = "Invited"
        ts.invited_at = now_datetime()
        ts.eligibility_snapshot = frappe.as_json(m["snapshot"])
        ts.insert(ignore_permissions=True)
        for pu in frappe.get_all("Supplier Portal User",
                                 filters={"parent": m["supplier"]},
                                 fields=["user"]):
            notification_service.send("supplier_invited", pu.user, {
                "tender": tender.name, "doctype": "Tender",
                "docname": tender.name})
        audit.log("supplier_invited", "Tender Supplier", ts.name,
                  metadata={"tender": tender.name, "supplier": m["supplier"]})


def close_tender(tender_name, reason="deadline"):
    """Idempotent closure — a double-run scheduler must be harmless."""
    tender = frappe.get_doc("Tender", tender_name)
    if tender.status in ("Closed", "Awarded", "Cancelled", "Expired"):
        return tender  # already terminal — nothing to do
    offer_count = frappe.db.count("Offer", {
        "tender": tender.name,
        "status": ["in", ["Submitted", "Shortlisted"]]})
    new_status = "Closed" if offer_count else "Expired"
    tender.flags.via_service = True
    tender.db_set("status", new_status)
    tender.db_set("closed_at", now_datetime())
    frappe.db.set_value("Rental Request", tender.rental_request, "status",
                        "Customer Reviewing" if offer_count else "Expired")
    audit.log("tender_closed", "Tender", tender.name,
              old_state="Open", new_state=new_status,
              metadata={"reason": reason, "offers": offer_count})
    return tender


def mark_viewed(tender_name, supplier):
    name = frappe.db.get_value("Tender Supplier",
                               {"tender": tender_name, "supplier": supplier})
    if name:
        row = frappe.get_doc("Tender Supplier", name)
        if not row.viewed_at:
            row.db_set("viewed_at", now_datetime())
            if row.invitation_status == "Invited":
                row.db_set("invitation_status", "Viewed")

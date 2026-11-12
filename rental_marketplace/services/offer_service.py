"""OfferService — supplier bids with immutable versions."""

import json

import frappe
from frappe.utils import now_datetime, get_datetime, add_to_date, date_diff

from rental_marketplace.services import audit, notification_service, pricing_service
from rental_marketplace.exceptions import (
    TenderClosed, SupplierNotEligible, OfferExpired)


def submit_offer(tender_name, supplier, vehicle, discount=0, extras=None,
                 deposit_amount=0, cancellation_policy=None,
                 mileage_policy="", fuel_policy="", insurance_summary="",
                 supplier_notes="", valid_hours=None):
    tender = frappe.get_doc("Tender", tender_name)
    if tender.status != "Open":
        raise TenderClosed()
    if get_datetime(tender.response_deadline) < now_datetime():
        raise TenderClosed("The response deadline has passed.")

    invited = frappe.db.get_value(
        "Tender Supplier", {"tender": tender_name, "supplier": supplier},
        ["name", "invitation_status"], as_dict=True)
    if not invited or invited.invitation_status in ("Declined", "Expired"):
        raise SupplierNotEligible()

    veh = frappe.db.get_value(
        "Marketplace Vehicle", vehicle,
        ["supplier", "vehicle_class", "active"], as_dict=True)
    if not veh or veh.supplier != supplier or not veh.active:
        raise SupplierNotEligible("Vehicle does not belong to this supplier.")
    if veh.vehicle_class != tender.vehicle_class:
        raise SupplierNotEligible("Vehicle class does not match the tender.")

    rental_days = max(date_diff(str(tender.dropoff_datetime)[:10],
                                str(tender.pickup_datetime)[:10]), 1)
    one_way = bool(tender.dropoff_location and
                   tender.dropoff_location != tender.pickup_location)

    extra_rows = []
    for ex in (extras or []):
        row = frappe.db.get_value("Marketplace Extra", ex.get("extra"),
                                  ["extra_name", "pricing_method", "price"],
                                  as_dict=True)
        if row:
            extra_rows.append({"extra": ex.get("extra"),
                               "quantity": ex.get("quantity") or 1,
                               "pricing_method": row.pricing_method,
                               "price": row.price})

    pricing = pricing_service.price_offer(
        supplier=supplier, vehicle_class=tender.vehicle_class,
        pickup_location=tender.pickup_location,
        pickup_date=str(tender.pickup_datetime)[:10],
        rental_days=rental_days, extras=extra_rows, discount=discount,
        one_way=one_way, deposit_amount=deposit_amount,
        currency=tender.currency, vehicle=vehicle)

    settings = frappe.get_single("Marketplace Settings")
    if not cancellation_policy:
        cancellation_policy = frappe.db.get_value(
            "Marketplace Supplier", supplier, "default_cancellation_policy")

    offer = frappe.new_doc("Offer")
    offer.tender = tender.name
    offer.supplier = supplier
    offer.vehicle = vehicle
    offer.vehicle_class = tender.vehicle_class
    offer.pickup_location = tender.pickup_location
    offer.dropoff_location = tender.dropoff_location
    offer.rental_days = rental_days
    offer.base_rate = pricing["base_amount"]
    offer.discount = pricing["discount_amount"]
    offer.extras_amount = pricing["extra_amount"]
    offer.delivery_fee = pricing["delivery_amount"]
    offer.one_way_fee = pricing["one_way_amount"]
    offer.tax_amount = pricing["tax_amount"]
    offer.deposit_amount = pricing["deposit_amount"]
    offer.total_amount = pricing["gross_amount"]
    offer.marketplace_commission = pricing["commission_amount"]
    offer.supplier_payout = pricing["supplier_amount"]
    offer.currency = pricing["currency"]
    offer.cancellation_policy = cancellation_policy
    offer.mileage_policy = mileage_policy
    offer.fuel_policy = fuel_policy
    offer.insurance_summary = insurance_summary
    offer.supplier_notes = supplier_notes
    offer.status = "Submitted"
    offer.submitted_at = now_datetime()
    offer.current_version = 1
    offer.valid_until = add_to_date(
        now_datetime(), hours=valid_hours or settings.offer_validity_hours or 48)
    offer.pricing_snapshot = json.dumps(pricing, default=str)
    for line in pricing.get("extra_lines") or []:
        offer.append("offer_extras", {
            "extra": line["extra"], "quantity": line["quantity"],
            "pricing_method": line["pricing_method"], "amount": line["amount"]})
    offer.insert(ignore_permissions=True)

    _write_version(offer, 1)

    ts = frappe.get_doc("Tender Supplier", invited.name)
    ts.db_set("invitation_status", "Responded")
    ts.db_set("responded_at", now_datetime())

    frappe.db.set_value("Rental Request", tender.rental_request,
                        "status", "Offers Received")
    audit.log("offer_submitted", "Offer", offer.name,
              new_state="Submitted",
              metadata={"tender": tender.name, "total": offer.total_amount})
    notification_service.send("offer_received", tender.customer, {
        "doctype": "Tender", "docname": tender.name})
    return offer


def create_version(offer_name, updates, user=None):
    """The only legal way to change a submitted offer's terms.
    Bumps current_version and appends an immutable Offer Version.
    Only the latest version is selectable; history remains for audit.
    """
    offer = frappe.get_doc("Offer", offer_name)
    if offer.status not in ("Submitted", "Shortlisted"):
        raise OfferExpired("Only live offers can be re-versioned.")
    tender_status = frappe.db.get_value("Tender", offer.tender, "status")
    if tender_status != "Open":
        raise TenderClosed()

    allowed = {"base_rate", "discount", "extras_amount", "delivery_fee",
               "one_way_fee", "tax_amount", "deposit_amount", "total_amount",
               "valid_until", "cancellation_policy", "mileage_policy",
               "fuel_policy", "supplier_notes"}
    old_version = offer.current_version
    for k, v in (updates or {}).items():
        if k in allowed:
            offer.set(k, v)
    offer.current_version = old_version + 1
    offer.flags.via_offer_service = True
    offer.save(ignore_permissions=True)
    _write_version(offer, offer.current_version)
    audit.log("offer_changed", "Offer", offer.name,
              old_state=f"v{old_version}", new_state=f"v{offer.current_version}",
              metadata={"changed": list((updates or {}).keys())})
    return offer


def withdraw_offer(offer_name, supplier):
    offer = frappe.get_doc("Offer", offer_name)
    if offer.supplier != supplier:
        raise SupplierNotEligible()
    if offer.status not in ("Submitted", "Shortlisted"):
        raise OfferExpired("Offer is no longer live.")
    offer.flags.via_offer_service = True
    offer.db_set("status", "Withdrawn")
    audit.log("offer_withdrawn", "Offer", offer.name,
              old_state="Submitted", new_state="Withdrawn")
    return offer


def _write_version(offer, version):
    v = frappe.new_doc("Offer Version")
    v.offer = offer.name
    v.version = version
    v.base_rate = offer.base_rate
    v.discount = offer.discount
    v.fees = (offer.delivery_fee or 0) + (offer.one_way_fee or 0) + \
             (offer.extras_amount or 0)
    v.tax = offer.tax_amount
    v.total = offer.total_amount
    v.deposit = offer.deposit_amount
    v.cancellation_policy = offer.cancellation_policy
    v.mileage_policy = offer.mileage_policy
    v.fuel_policy = offer.fuel_policy
    v.valid_until = offer.valid_until
    v.created_by_user = frappe.session.user
    v.created_at = now_datetime()
    v.snapshot = json.dumps({
        f: str(offer.get(f)) for f in
        ("base_rate", "discount", "extras_amount", "delivery_fee",
         "one_way_fee", "tax_amount", "deposit_amount", "total_amount",
         "marketplace_commission", "supplier_payout", "currency")},
        default=str)
    v.insert(ignore_permissions=True)

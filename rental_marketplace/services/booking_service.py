"""BookingService — concurrency-safe acceptance + explicit state machine.

Never `booking.booking_status = X; booking.save()` — the Booking
controller rejects that. All transitions go through transition().
"""

import json

import frappe
from frappe.utils import now_datetime, get_datetime

from rental_marketplace.services import audit, notification_service
from rental_marketplace.exceptions import (
    InvalidStateTransition, OfferExpired, TenderClosed, VehicleUnavailable,
    BookingNotFound)

OPERATOR_ROLES = {"Marketplace Admin", "Marketplace Operations", "System Manager"}
SUPPLIER_ROLES = {"Supplier Admin", "Supplier Manager", "Supplier Agent"}

# action → allowed source states, target, who may perform it
TRANSITIONS = {
    "confirm": {
        "from": ["Pending Payment"], "to": "Confirmed",
        "roles": OPERATOR_ROLES | {"__system__"},
        "notify": "booking_confirmed",
    },
    "supplier_confirm": {
        "from": ["Confirmed"], "to": "Supplier Confirmed",
        "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "ready": {
        "from": ["Supplier Confirmed"], "to": "Ready for Pickup",
        "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "pickup": {
        "from": ["Ready for Pickup", "Supplier Confirmed"], "to": "Picked Up",
        "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "activate": {
        "from": ["Picked Up"], "to": "Active Rental",
        "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "return": {
        "from": ["Active Rental", "Picked Up"], "to": "Returned",
        "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "complete": {
        "from": ["Returned"], "to": "Completed",
        "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "no_show": {
        "from": ["Confirmed", "Supplier Confirmed", "Ready for Pickup"],
        "to": "No Show", "roles": OPERATOR_ROLES | SUPPLIER_ROLES,
    },
    "dispute": {
        "from": ["Picked Up", "Active Rental", "Returned", "Completed"],
        "to": "Disputed", "roles": OPERATOR_ROLES,
    },
    "cancel": {
        # reached only via cancellation_service, which sets flags
        "from": ["Pending Payment", "Confirmed", "Supplier Confirmed",
                 "Ready for Pickup"],
        "to": "Cancelled", "roles": OPERATOR_ROLES | {"__system__"},
        "notify": "booking_cancelled",
    },
}


def transition(booking, action, system=False, metadata=None):
    """Single domain-level transition mechanism."""
    if isinstance(booking, str):
        booking = frappe.get_doc("Booking", booking)
        if not booking:
            raise BookingNotFound()
    spec = TRANSITIONS.get(action)
    if not spec:
        raise InvalidStateTransition(f"Unknown action: {action}")
    if booking.booking_status not in spec["from"]:
        raise InvalidStateTransition(
            f"Cannot {action} a booking in state {booking.booking_status}.")
    if not system:
        roles = set(frappe.get_roles())
        if not (roles & spec["roles"]):
            frappe.throw("Not permitted to perform this transition.",
                         frappe.PermissionError)

    old = booking.booking_status
    booking.booking_status = spec["to"]
    if action == "confirm":
        booking.confirmed_at = now_datetime()
        _post_confirmation_ledger(booking)
    if action == "complete":
        booking.completed_at = now_datetime()
    if action in ("cancel", "reject"):
        booking.cancelled_at = now_datetime()
        booking.cancellation_status = "Processed"
    booking.flags.via_transition = True
    booking.save(ignore_permissions=True)

    audit.log(f"booking_{action}", "Booking", booking.name,
              old_state=old, new_state=spec["to"], metadata=metadata)
    if spec.get("notify"):
        notification_service.send(spec["notify"], booking.customer, {
            "booking": booking.name, "doctype": "Booking",
            "docname": booking.name})
    if action == "confirm":
        for pu in frappe.get_all("Supplier Portal User",
                                 filters={"parent": booking.supplier},
                                 fields=["user"]):
            notification_service.send("supplier_confirmation_required",
                                      pu.user, {"booking": booking.name})
    return booking


def accept_offer(tender_name, offer_name, customer_user):
    """Concurrency-safe acceptance.

    BEGIN → lock tender → lock offer → lock availability window →
    create booking + reservation → accept offer / reject competitors /
    award tender → COMMIT (Frappe commits the request transaction).

    Never trusts vehicle.status — availability is derived from blocks
    and conflicting bookings inside the same locked read.
    """
    tender_row = frappe.db.sql(
        "SELECT name, status, customer, rental_request, pickup_datetime, "
        "dropoff_datetime, pickup_location, dropoff_location "
        "FROM `tabTender` WHERE name=%s FOR UPDATE",
        tender_name, as_dict=True)
    if not tender_row:
        raise TenderClosed("Tender not found.")
    tender = tender_row[0]
    if tender.customer != customer_user and \
            not (set(frappe.get_roles()) & OPERATOR_ROLES):
        frappe.throw("Not your tender.", frappe.PermissionError)
    if tender.status not in ("Open", "Closing", "Closed"):
        raise TenderClosed()

    offer_row = frappe.db.sql(
        "SELECT name, status, valid_until, supplier, vehicle "
        "FROM `tabOffer` WHERE name=%s AND tender=%s FOR UPDATE",
        (offer_name, tender_name), as_dict=True)
    if not offer_row:
        raise OfferExpired("Offer not found on this tender.")
    offer_head = offer_row[0]
    if offer_head.status not in ("Submitted", "Shortlisted"):
        raise OfferExpired()
    if offer_head.valid_until and \
            get_datetime(offer_head.valid_until) < now_datetime():
        raise OfferExpired()

    # Lock the availability window: existing blocks for this vehicle
    conflicts = frappe.db.sql(
        """
        SELECT name FROM `tabVehicle Availability Block`
        WHERE vehicle=%(vehicle)s AND status='Active'
          AND start_datetime < %(end)s AND end_datetime > %(start)s
        FOR UPDATE
        """,
        {"vehicle": offer_head.vehicle,
         "start": tender.pickup_datetime, "end": tender.dropoff_datetime})
    if conflicts:
        raise VehicleUnavailable()
    conflicting_booking = frappe.db.sql(
        """
        SELECT name FROM `tabBooking`
        WHERE vehicle=%(vehicle)s
          AND booking_status NOT IN ('Cancelled','No Show','Completed')
          AND pickup_datetime < %(end)s AND dropoff_datetime > %(start)s
        FOR UPDATE
        """,
        {"vehicle": offer_head.vehicle,
         "start": tender.pickup_datetime, "end": tender.dropoff_datetime})
    if conflicting_booking:
        raise VehicleUnavailable()

    offer = frappe.get_doc("Offer", offer_name)
    booking = _create_booking_from_offer(offer, tender)

    # reservation block inside the same transaction
    block = frappe.new_doc("Vehicle Availability Block")
    block.vehicle = offer.vehicle
    block.block_type = "Booking"
    block.status = "Active"
    block.start_datetime = tender.pickup_datetime
    block.end_datetime = tender.dropoff_datetime
    block.booking = booking.name
    block.reason = f"Reservation for {booking.name}"
    block.flags.skip_overlap_check = True  # window already locked above
    block.insert(ignore_permissions=True)
    booking.db_set("availability_block", block.name)

    offer.flags.via_offer_service = True
    offer.db_set("status", "Accepted")
    for other in frappe.get_all(
            "Offer", filters={"tender": tender_name,
                              "name": ["!=", offer_name],
                              "status": ["in", ["Submitted", "Shortlisted"]]},
            pluck="name"):
        frappe.db.set_value("Offer", other, "status", "Rejected")

    frappe.db.set_value("Tender", tender_name,
                        {"status": "Awarded", "closed_at": now_datetime()})
    frappe.db.set_value("Rental Request", tender.rental_request,
                        "status", "Accepted")

    audit.log("offer_accepted", "Offer", offer.name,
              old_state=offer_head.status, new_state="Accepted",
              metadata={"tender": tender_name, "booking": booking.name})
    audit.log("booking_created", "Booking", booking.name,
              new_state="Pending Payment",
              metadata={"offer": offer.name, "total": booking.gross_amount})
    notification_service.send("offer_accepted", _supplier_user(offer.supplier),
                              {"booking": booking.name})
    return booking


def create_direct_booking(vehicle, pickup_datetime, dropoff_datetime,
                          customer_user, pickup_location=None,
                          dropoff_location=None):
    """Customer picks a specific vehicle straight off the fleet page,
    no tender/bidding round. Still locks the availability window the
    same way accept_offer() does, and still needs admin confirm/approval
    downstream (booking starts at Pending Payment same as normal flow).
    """
    from frappe.utils import date_diff
    from rental_marketplace.services import pricing_service

    veh = frappe.db.sql(
        "SELECT name, supplier, vehicle_class, active, display_name, "
        "make, model, model_year, transmission, fuel_type, seats "
        "FROM `tabMarketplace Vehicle` WHERE name=%s FOR UPDATE",
        vehicle, as_dict=True)
    if not veh or not veh[0].active:
        raise VehicleUnavailable("Vehicle not available.")
    veh = veh[0]

    conflicts = frappe.db.sql(
        """
        SELECT name FROM `tabVehicle Availability Block`
        WHERE vehicle=%(vehicle)s AND status='Active'
          AND start_datetime < %(end)s AND end_datetime > %(start)s
        FOR UPDATE
        """,
        {"vehicle": vehicle, "start": pickup_datetime, "end": dropoff_datetime})
    if conflicts:
        raise VehicleUnavailable()
    conflicting_booking = frappe.db.sql(
        """
        SELECT name FROM `tabBooking`
        WHERE vehicle=%(vehicle)s
          AND booking_status NOT IN ('Cancelled','No Show','Completed')
          AND pickup_datetime < %(end)s AND dropoff_datetime > %(start)s
        FOR UPDATE
        """,
        {"vehicle": vehicle, "start": pickup_datetime, "end": dropoff_datetime})
    if conflicting_booking:
        raise VehicleUnavailable()

    rental_days = max(date_diff(str(dropoff_datetime)[:10],
                                str(pickup_datetime)[:10]), 1)
    pickup_location = pickup_location or ""
    dropoff_location = dropoff_location or pickup_location

    pricing = pricing_service.price_offer(
        supplier=veh.supplier, vehicle_class=veh.vehicle_class,
        pickup_location=pickup_location,
        pickup_date=str(pickup_datetime)[:10],
        rental_days=rental_days, vehicle=vehicle)

    customer_profile = frappe.db.get_value(
        "Customer Profile", {"user": customer_user},
        ["first_name", "last_name", "phone", "country"], as_dict=True) or {}
    supplier_snap = frappe.db.get_value(
        "Marketplace Supplier", veh.supplier,
        ["supplier_name", "contact_email", "contact_phone", "rating"],
        as_dict=True) or {}
    vehicle_snap = {"display_name": veh.display_name, "make": veh.make,
                    "model": veh.model, "model_year": veh.model_year,
                    "transmission": veh.transmission,
                    "fuel_type": veh.fuel_type, "seats": veh.seats,
                    "vehicle_class": veh.vehicle_class}

    b = frappe.new_doc("Booking")
    b.flags.via_service = True
    b.customer = customer_user
    b.supplier = veh.supplier
    b.vehicle = vehicle
    b.pickup_location = pickup_location
    b.dropoff_location = dropoff_location
    b.pickup_datetime = pickup_datetime
    b.dropoff_datetime = dropoff_datetime
    b.currency = pricing["currency"]
    b.base_amount = pricing["base_amount"]
    b.discount_amount = pricing["discount_amount"]
    b.extras_amount = pricing["extra_amount"]
    b.fees_amount = (pricing["delivery_amount"] or 0) + (pricing["one_way_amount"] or 0)
    b.tax_amount = pricing["tax_amount"]
    b.gross_amount = pricing["gross_amount"]
    b.commission_amount = pricing["commission_amount"]
    b.supplier_amount = pricing["supplier_amount"]
    b.deposit_amount = pricing["deposit_amount"]
    b.amount_paid = 0
    b.booking_status = "Pending Payment"
    b.payment_status = "Unpaid"
    b.customer_snapshot = json.dumps(
        {"user": customer_user, **{k: str(v) for k, v in
                                   (customer_profile or {}).items()}},
        default=str)
    b.supplier_snapshot = json.dumps(supplier_snap, default=str)
    b.vehicle_snapshot = json.dumps(vehicle_snap, default=str)
    b.pricing_snapshot = json.dumps(pricing, default=str)
    b.insert(ignore_permissions=True)
    b.db_set("booking_reference", b.name)

    block = frappe.new_doc("Vehicle Availability Block")
    block.vehicle = vehicle
    block.block_type = "Booking"
    block.status = "Active"
    block.start_datetime = pickup_datetime
    block.end_datetime = dropoff_datetime
    block.booking = b.name
    block.reason = f"Reservation for {b.name}"
    block.flags.skip_overlap_check = True
    block.insert(ignore_permissions=True)
    b.db_set("availability_block", block.name)

    audit.log("booking_created", "Booking", b.name,
              new_state="Pending Payment",
              metadata={"direct_booking": True, "total": b.gross_amount})
    notification_service.send("offer_accepted", _supplier_user(veh.supplier),
                              {"booking": b.name})
    return b


def _create_booking_from_offer(offer, tender):
    customer_profile = frappe.db.get_value(
        "Customer Profile", {"user": tender.customer},
        ["first_name", "last_name", "phone", "country"], as_dict=True) or {}
    supplier_snap = frappe.db.get_value(
        "Marketplace Supplier", offer.supplier,
        ["supplier_name", "contact_email", "contact_phone", "rating"],
        as_dict=True) or {}
    vehicle_snap = frappe.db.get_value(
        "Marketplace Vehicle", offer.vehicle,
        ["display_name", "make", "model", "model_year", "transmission",
         "fuel_type", "seats", "vehicle_class"], as_dict=True) or {}
    policy_snap = {}
    if offer.cancellation_policy:
        policy_snap = frappe.db.get_value(
            "Cancellation Policy", offer.cancellation_policy,
            ["policy_name", "policy_type", "free_cancellation_hours",
             "penalty_type", "penalty_value", "no_show_penalty"],
            as_dict=True) or {}

    b = frappe.new_doc("Booking")
    b.flags.via_service = True
    b.customer = tender.customer
    b.supplier = offer.supplier
    b.vehicle = offer.vehicle
    b.accepted_offer = offer.name
    b.rental_request = tender.rental_request
    b.pickup_location = tender.pickup_location
    b.dropoff_location = tender.dropoff_location
    b.pickup_datetime = tender.pickup_datetime
    b.dropoff_datetime = tender.dropoff_datetime
    b.currency = offer.currency
    b.base_amount = offer.base_rate
    b.discount_amount = offer.discount
    b.extras_amount = offer.extras_amount
    b.fees_amount = (offer.delivery_fee or 0) + (offer.one_way_fee or 0)
    b.tax_amount = offer.tax_amount
    b.gross_amount = offer.total_amount
    b.commission_amount = offer.marketplace_commission
    b.supplier_amount = offer.supplier_payout
    b.deposit_amount = offer.deposit_amount
    # partner attribution flows request → booking
    partner = frappe.db.get_value("Rental Request", tender.rental_request,
                                  "partner")
    if partner:
        share = frappe.db.get_value("Marketplace Partner", partner,
                                    "commission_share_percentage") or 0
        b.partner = partner
        b.partner_commission = round(
            (offer.marketplace_commission or 0) * share / 100.0, 2)
    b.amount_paid = 0
    # No admin-approval gate: booking goes straight to Pending Payment
    # the moment the customer accepts the supplier's offer.
    b.booking_status = "Pending Payment"
    b.payment_status = "Unpaid"
    b.customer_snapshot = json.dumps(
        {"user": tender.customer, **{k: str(v) for k, v in
                                     (customer_profile or {}).items()}},
        default=str)
    b.supplier_snapshot = json.dumps(supplier_snap, default=str)
    b.vehicle_snapshot = json.dumps(vehicle_snap, default=str)
    b.pricing_snapshot = offer.pricing_snapshot
    b.policy_snapshot = json.dumps(policy_snap, default=str)
    b.insert(ignore_permissions=True)
    b.db_set("booking_reference", b.name)
    return b


def _post_confirmation_ledger(booking):
    """Marketplace ledger on confirmation: receipt / commission /
    supplier payable. Idempotent per booking."""
    if frappe.db.exists("Marketplace Ledger Entry",
                        {"booking": booking.name,
                         "entry_type": "Customer Receipt"}):
        return
    # Commission-only booking: the customer paid commission + tax to the
    # platform and pays the supplier directly, so the platform owes the
    # supplier nothing. Full-payment booking: platform holds the whole amount.
    full = bool(booking.get("collect_full_amount"))
    received = (booking.gross_amount if full else
                (booking.commission_amount or 0) + (booking.tax_amount or 0))
    entries = [
        ("Customer Receipt", received, None),
        ("Marketplace Commission", booking.commission_amount, None),
        ("Partner Commission", booking.get("partner_commission"),
         booking.get("partner")),
    ]
    if full:
        entries.insert(2, ("Supplier Payable", booking.supplier_amount, None))
    for entry_type, amount, partner in entries:
        if not amount:
            continue
        e = frappe.new_doc("Marketplace Ledger Entry")
        e.posting_datetime = now_datetime()
        e.entry_type = entry_type
        e.booking = booking.name
        e.supplier = booking.supplier
        e.partner = partner
        e.amount = amount
        e.currency = booking.currency
        e.reference_doctype = "Booking"
        e.reference_name = booking.name
        e.insert(ignore_permissions=True)


def _supplier_user(supplier):
    users = frappe.get_all("Supplier Portal User",
                           filters={"parent": supplier}, fields=["user"])
    return users[0].user if users else "Administrator"


def on_booking_update(doc, method=None):
    """hooks doc_event — keep payment_status coherent with amounts.
    Platform only ever collects its own share (commission + tax) —
    the rest is paid to the supplier directly, so paid-in-full means
    amount_paid has covered that share, not the full gross_amount."""
    platform_total = (doc.commission_amount or 0) + (doc.tax_amount or 0)
    if platform_total and doc.amount_paid is not None:
        if doc.amount_paid <= 0:
            new = "Unpaid"
        elif doc.amount_paid < platform_total:
            new = "Partially Paid"
        else:
            new = "Paid"
        if doc.payment_status not in ("Refunded", "Partially Refunded") \
                and doc.payment_status != new:
            frappe.db.set_value("Booking", doc.name, "payment_status", new,
                                update_modified=False)

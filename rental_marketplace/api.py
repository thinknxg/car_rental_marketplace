"""Deliberately small public API surface.

Every state-changing endpoint: authenticates, authorizes the object,
validates transitions and money server-side, supports idempotency
keys, emits audit events, and returns a stable error structure:

    {"success": false, "error": {"code": "...", "message": "..."}}

No generic CRUD is exposed for commercially sensitive DocTypes.
"""

import functools
import json

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime, get_datetime

from rental_marketplace.exceptions import (
    MarketplaceError, BookingNotFound, TenderClosed)
from rental_marketplace.services import (
    audit, idempotency, tender_service, offer_service, booking_service,
    payment_service, cancellation_service)
from rental_marketplace import permissions as perms

RANKING_VERSION = "rank-v1"


def api_handler(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        kwargs.pop("cmd", None)
        try:
            return {"success": True, "data": fn(*args, **kwargs)}
        except MarketplaceError as e:
            frappe.local.response["http_status_code"] = e.http_status
            frappe.db.rollback()
            return {"success": False,
                    "error": {"code": e.code, "message": e.message}}
        except frappe.PermissionError as e:
            frappe.local.response["http_status_code"] = 403
            frappe.db.rollback()
            return {"success": False,
                    "error": {"code": "PERMISSION_DENIED",
                              "message": "You are not permitted to do this."}}
        except frappe.ValidationError as e:
            frappe.local.response["http_status_code"] = 400
            frappe.db.rollback()
            return {"success": False,
                    "error": {"code": "VALIDATION_ERROR", "message": str(e)}}
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"API error: {fn.__name__}")
            frappe.local.response["http_status_code"] = 500
            frappe.db.rollback()
            return {"success": False,
                    "error": {"code": "INTERNAL_ERROR",
                              "message": "An unexpected error occurred."}}
    return wrapper


def _require_login():
    if frappe.session.user == "Guest":
        frappe.throw("Login required.", frappe.PermissionError)


# ── Supplier onboarding ─────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=True)
@rate_limit(limit=10, seconds=3600)
def upload_supplier_document():
    """Guest-safe file upload for the supplier application form.
    Frappe's built-in upload_file endpoint requires a logged-in user,
    but applicants here are anonymous — so we save the file privately
    ourselves and just return its URL."""
    file = frappe.request.files.get("file")
    if not file:
        frappe.throw("No file provided", frappe.ValidationError)
    from frappe.utils.file_manager import save_file
    import os
    if os.path.splitext(file.filename or "")[1].lower() not in (".pdf", ".jpg", ".jpeg", ".png"):
        frappe.throw("Only PDF, JPG or PNG files are allowed.", frappe.ValidationError)
    content = file.stream.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        frappe.throw("File is too large (max 5 MB).", frappe.ValidationError)
    saved = save_file(file.filename, content, None, None, is_private=1)
    return {"file_url": saved.file_url}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=30, seconds=3600)
@api_handler
def submit_supplier_application(business_name, legal_name=None, contact_email=None,
                                contact_phone=None, country=None,
                                fleet_size=None, notes=None, locations=None,
                                business_type=None, business_registration_number=None,
                                tax_registration_number=None, business_address=None,
                                contact_person_name=None, trade_license=None,
                                id_document=None, insurance_certificate=None,
                                company_logo=None):
    import json as _json
    business_name = (business_name or "").strip()
    contact_email = (contact_email or "").strip().lower()
    if not business_name or not contact_email:
        frappe.throw("Business name and email are required.",
                     frappe.ValidationError)

    if frappe.db.exists("Marketplace Supplier", {"contact_email": contact_email}):
        frappe.throw("An application with this email already exists. "
                     "Our team will be in touch shortly.")

    if isinstance(locations, str):
        try:
            locations = _json.loads(locations)
        except ValueError:
            locations = []
    locations = locations or []

    try:
        fleet_size_int = int(fleet_size) if fleet_size not in (None, "") else None
    except (TypeError, ValueError):
        fleet_size_int = None

    sup = frappe.new_doc("Marketplace Supplier")
    sup.supplier_name = business_name
    sup.legal_name = legal_name or business_name
    sup.business_type = business_type
    sup.business_registration_number = business_registration_number
    sup.tax_registration_number = tax_registration_number
    sup.business_address = business_address
    sup.declared_fleet_size = fleet_size_int
    sup.contact_person_name = contact_person_name
    sup.contact_email = contact_email
    sup.contact_phone = contact_phone
    sup.country = country
    sup.application_notes = notes
    sup.status = "Pending Verification"
    sup.trade_license = trade_license
    sup.id_document = id_document
    sup.insurance_certificate = insurance_certificate
    sup.company_logo = company_logo
    for loc in locations:
        if frappe.db.exists("Marketplace Location", loc):
            sup.append("service_areas", {"location": loc})
    sup.insert(ignore_permissions=True)

    audit.log("supplier_application_submitted", "Marketplace Supplier",
              sup.name, metadata={"fleet_size": fleet_size_int, "notes": notes,
                                   "locations": locations})
    return {"supplier": sup.name}


# ── Customer endpoints ─────────────────────────────────────────────────────────

def _get_or_create_guest_customer(kwargs):
    email = (kwargs.get("customer_email") or "").strip().lower()
    full_name = (kwargs.get("customer_name") or "").strip()
    phone = (kwargs.get("customer_phone") or "").strip()
    if not email or not full_name:
        frappe.throw("Name and email are required.", frappe.ValidationError)
    if frappe.db.exists("User", email):
        frappe.throw("An account with this email already exists. Please log in "
                     "(or use 'Forgot password') and try again.",
                     frappe.ValidationError)
    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": full_name.split(" ")[0],
        "last_name": " ".join(full_name.split(" ")[1:]) or "",
        "mobile_no": phone,
        "send_welcome_email": 0,
        "user_type": "Website User",
    })
    user.insert(ignore_permissions=True)
    user.add_roles("Car Marketplace Customer")
    frappe.db.commit()
    return user.name


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=30, seconds=3600)
@api_handler
def create_direct_booking(vehicle, pickup_datetime, dropoff_datetime,
                          customer_name, customer_email, customer_phone,
                          pickup_location=None, dropoff_location=None,
                          idempotency_key=None):
    from rental_marketplace.services import booking_service
    if frappe.session.user == "Guest":
        customer_user = _get_or_create_guest_customer({
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone})
        frappe.local.login_manager.login_as(customer_user)
    else:
        customer_user = frappe.session.user

    def _do():
        booking = booking_service.create_direct_booking(
            vehicle, pickup_datetime, dropoff_datetime, customer_user,
            pickup_location=pickup_location, dropoff_location=dropoff_location)
        return {"booking": booking.name,
                "booking_status": booking.booking_status,
                "amount_due": booking.gross_amount,
                "currency": booking.currency}

    return idempotency.run(idempotency_key, "create_direct_booking",
                           {"vehicle": vehicle, "pickup": pickup_datetime,
                            "dropoff": dropoff_datetime,
                            "email": customer_email}, _do)


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=30, seconds=3600)
@api_handler
def create_rental_request(**kwargs):
    if frappe.session.user == "Guest":
        customer_user = _get_or_create_guest_customer(kwargs)
        frappe.local.login_manager.login_as(customer_user)
    else:
        customer_user = frappe.session.user
    fields = ("pickup_location", "dropoff_location", "pickup_datetime",
              "dropoff_datetime", "same_return_location", "vehicle_class",
              "preferred_make", "preferred_model", "transmission",
              "fuel_type", "seats_required", "driver_age",
              "country_of_residence", "special_requirements", "currency")
    doc = frappe.new_doc("Car Rental Request")
    doc.customer = customer_user
    for f in fields:
        if kwargs.get(f) not in (None, ""):
            doc.set(f, kwargs[f])
    for ex in json.loads(kwargs.get("extras") or "[]"):
        doc.append("extras", {"extra": ex.get("extra"),
                              "quantity": ex.get("quantity") or 1})
    # partner referral attribution (code → Active partner)
    ref = (kwargs.get("referral_code") or "").strip().upper()
    if ref:
        partner = frappe.db.get_value(
            "Marketplace Partner", {"partner_code": ref, "status": "Active"})
        if partner:
            doc.referral_code = ref
            doc.partner = partner
    doc.status = "Submitted"
    doc.insert(ignore_permissions=True)
    audit.log("rental_request_submitted", "Car Rental Request", doc.name)

    tender = tender_service.create_tender_from_request(doc.name)
    return {"request": doc.name, "tender": tender.name,
            "response_deadline": str(tender.response_deadline)}


@frappe.whitelist(allow_guest=False)
@api_handler
def get_rental_request(request_id):
    _require_login()
    doc = frappe.get_doc("Car Rental Request", request_id)
    if doc.customer != frappe.session.user and \
            not perms.is_operator(frappe.session.user):
        frappe.throw("Not your request.", frappe.PermissionError)
    tender = frappe.db.get_value("Tender", {"rental_request": doc.name},
                                 ["name", "status", "response_deadline"],
                                 as_dict=True)
    return {"request": doc.as_dict(convert_dates_to_str=True),
            "tender": tender}


@frappe.whitelist(allow_guest=False)
@api_handler
def get_tender_offers(tender_id, sort="recommended"):
    """Normalized offer cards — customers never see raw supplier
    records or the commission split."""
    _require_login()
    tender = frappe.get_doc("Tender", tender_id)
    if tender.customer != frappe.session.user and \
            not perms.is_operator(frappe.session.user):
        frappe.throw("Not your tender.", frappe.PermissionError)

    settings = frappe.get_single("Car Marketplace Settings")
    offers = frappe.get_all(
        "Offer",
        filters={"tender": tender_id,
                 "status": ["in", ["Submitted", "Shortlisted"]]},
        fields=["name", "supplier", "vehicle", "total_amount",
                "deposit_amount", "currency", "cancellation_policy",
                "mileage_policy", "fuel_policy", "insurance_summary",
                "valid_until", "rental_days", "current_version"])
    if not offers:
        return {"tender": tender_id, "offers": [],
                "response_deadline": str(tender.response_deadline),
                "ranking_version": RANKING_VERSION}

    prices = [o.total_amount for o in offers if o.total_amount]
    min_p, max_p = (min(prices), max(prices)) if prices else (0, 0)
    cards = []
    for o in offers:
        veh = frappe.db.get_value(
            "Marketplace Vehicle", o.vehicle,
            ["display_name", "vehicle_class", "transmission", "fuel_type",
             "seats", "primary_photo"], as_dict=True) or frappe._dict()
        sup = frappe.db.get_value(
            "Marketplace Supplier", o.supplier,
            ["supplier_name", "rating", "reliability_score", "status"],
            as_dict=True) or frappe._dict()
        policy = frappe.db.get_value(
            "Cancellation Policy", o.cancellation_policy,
            ["policy_type", "free_cancellation_hours"], as_dict=True) \
            or frappe._dict()

        price_score = 1.0 if max_p == min_p else \
            1.0 - (o.total_amount - min_p) / (max_p - min_p)
        flex = {"Fully Flexible": 1.0, "Partially Refundable": 0.6,
                "Supplier Specific": 0.4, "Non Refundable": 0.0
                }.get(policy.policy_type, 0.5)
        score = round(
            price_score * (settings.w_price or 0.35)
            + ((sup.rating or 3.5) / 5.0) * (settings.w_rating or 0.20)
            + 1.0 * (settings.w_match or 0.20)
            + flex * (settings.w_cancellation or 0.10)
            + (sup.reliability_score or 0.8) * (settings.w_reliability or 0.10)
            + 1.0 * (settings.w_pickup or 0.05), 4)

        cards.append({
            "offer": o.name,
            "vehicle_name": veh.display_name,
            "vehicle_class": veh.vehicle_class,
            "transmission": veh.transmission,
            "fuel": veh.fuel_type,
            "seats": veh.seats,
            "photo": veh.primary_photo,
            "supplier_display": sup.supplier_name,
            "supplier_rating": sup.rating,
            "supplier_verified": sup.status == "Verified",
            "cancellation_summary": policy.policy_type or "See terms",
            "free_cancellation_hours": policy.free_cancellation_hours,
            "mileage_policy": o.mileage_policy,
            "fuel_policy": o.fuel_policy,
            "insurance_summary": o.insurance_summary,
            "price": o.total_amount,
            "deposit": o.deposit_amount,
            "currency": o.currency,
            "rental_days": o.rental_days,
            "valid_until": str(o.valid_until or ""),
            "version": o.current_version,
            "score": score,
        })

    keys = {
        "recommended": lambda c: -c["score"],
        "lowest_price": lambda c: c["price"] or 0,
        "best_rated": lambda c: -(c["supplier_rating"] or 0),
        "flexible_cancellation": lambda c: -(
            1 if c["cancellation_summary"] == "Fully Flexible" else 0),
    }
    cards.sort(key=keys.get(sort, keys["recommended"]))
    return {"tender": tender_id, "offers": cards,
            "response_deadline": str(tender.response_deadline),
            "ranking_version": RANKING_VERSION, "sort": sort}


@frappe.whitelist(allow_guest=False)
@api_handler
def accept_offer(tender_id, offer_id, idempotency_key=None):
    _require_login()

    def _do():
        booking = booking_service.accept_offer(
            tender_id, offer_id, frappe.session.user)
        return {"booking": booking.name,
                "booking_status": booking.booking_status,
                "amount_due": booking.amount_due,
                "currency": booking.currency}

    return idempotency.run(idempotency_key, "accept_offer",
                           {"tender": tender_id, "offer": offer_id}, _do)


@frappe.whitelist(allow_guest=False)
@api_handler
def get_booking(booking_id):
    _require_login()
    booking = frappe.get_doc("Booking", booking_id)
    supplier = perms.get_user_supplier(frappe.session.user)
    if booking.customer != frappe.session.user \
            and booking.supplier != supplier \
            and not perms.is_operator(frappe.session.user):
        raise BookingNotFound()  # do not leak existence
    d = booking.as_dict(convert_dates_to_str=True)
    if booking.customer == frappe.session.user and \
            not perms.is_operator(frappe.session.user):
        for private in ("commission_amount", "supplier_amount",
                        "supplier_snapshot"):
            d.pop(private, None)
    return d


@frappe.whitelist(allow_guest=False)
@api_handler
def cancel_booking(booking_id, reason="", idempotency_key=None):
    _require_login()

    def _do():
        return cancellation_service.cancel_booking(
            booking_id, frappe.session.user,
            reason=reason or "Customer cancellation")

    return idempotency.run(idempotency_key, "cancel_booking",
                           {"booking": booking_id}, _do)


@frappe.whitelist(allow_guest=False)
@api_handler
def create_payment(booking_id, idempotency_key=None, full_payment=0):
    _require_login()
    full_payment = bool(int(full_payment or 0))

    def _do():
        intent = payment_service.create_payment_intent(
            booking_id, frappe.session.user, full_payment=full_payment)
        # DEMO ONLY — simulates a successful payment immediately so the
        # full flow (confirm/ledger/payment status) can be tested without
        # a real gateway. Remove this call once one is wired in.
        if frappe.conf.get("allow_demo_payments"):
            payment_service.simulate_demo_payment(intent.name)
        return {"payment_intent": intent.name,
                "provider_intent_id": intent.provider_intent_id,
                "amount": intent.amount, "currency": intent.currency}

    return idempotency.run(idempotency_key, "create_payment",
                           {"booking": booking_id}, _do)


@frappe.whitelist(allow_guest=False)
@api_handler
def start_paypal_payment(booking_id, idempotency_key=None, full_payment=0):
    _require_login()
    full_payment = bool(int(full_payment or 0))

    def _do():
        approval_url = payment_service.start_paypal_checkout(
            booking_id, frappe.session.user, full_payment=full_payment)
        return {"approval_url": approval_url}

    return idempotency.run(idempotency_key, "start_paypal_payment",
                           {"booking": booking_id}, _do)


@frappe.whitelist(allow_guest=False)
@api_handler
def preview_offer_price(tender_id, vehicle, discount=0, deposit_amount=0):
    """Live price preview for the supplier's offer form — same math as
    submit_offer, but nothing is saved. Never returns commission/payout
    (Marketplace Split is private to admin/finance)."""
    from rental_marketplace.services import pricing_service
    _require_login()
    supplier = perms.get_user_supplier(frappe.session.user)
    if not supplier:
        frappe.throw("No supplier linked to this user.", frappe.PermissionError)

    tender = frappe.get_doc("Tender", tender_id)
    veh = frappe.db.get_value(
        "Marketplace Vehicle", vehicle,
        ["supplier", "vehicle_class", "active"], as_dict=True)
    if not veh or veh.supplier != supplier or not veh.active:
        frappe.throw("Vehicle does not belong to this supplier.",
                     frappe.PermissionError)
    if veh.vehicle_class != tender.vehicle_class:
        frappe.throw("Vehicle class does not match the tender.")

    from frappe.utils import date_diff
    rental_days = max(date_diff(str(tender.dropoff_datetime)[:10],
                                str(tender.pickup_datetime)[:10]), 1)
    one_way = bool(tender.dropoff_location and
                   tender.dropoff_location != tender.pickup_location)

    pricing = pricing_service.price_offer(
        supplier=supplier, vehicle_class=tender.vehicle_class,
        pickup_location=tender.pickup_location,
        pickup_date=str(tender.pickup_datetime)[:10],
        rental_days=rental_days, discount=float(discount or 0),
        one_way=one_way, deposit_amount=float(deposit_amount or 0),
        currency=tender.currency, vehicle=vehicle)

    return {"rental_days": rental_days,
            "base_amount": pricing["base_amount"],
            "discount_amount": pricing["discount_amount"],
            "tax_amount": pricing["tax_amount"],
            "total_amount": pricing["gross_amount"],
            "currency": tender.currency}


@frappe.whitelist(allow_guest=True)
@api_handler
def list_public_vehicles(vehicle_class=None, make=None, model=None,
                         pickup_datetime=None, dropoff_datetime=None):
    """Public fleet listing — safe fields only, no supplier-private data.
    Marketplace Vehicle has no Guest read permission, so this is the only
    way the public fleet page can see vehicles.

    When pickup_datetime/dropoff_datetime are given, each vehicle gets an
    "available" boolean computed the same way accept_offer checks
    conflicts — active Vehicle Availability Blocks and non-cancelled
    Bookings overlapping the requested window."""
    filters = {"active": 1}
    if vehicle_class:
        filters["vehicle_class"] = vehicle_class
    if make:
        filters["make"] = make
    if model:
        filters["model"] = ["like", f"%{model}%"]
    vehicles = frappe.get_all(
        "Marketplace Vehicle", filters=filters,
        fields=["name", "display_name", "make", "model", "model_year",
                "vehicle_class", "transmission", "fuel_type", "seats",
                "primary_photo", "supplier", "daily_rate",
                "weekly_rate", "monthly_rate"],
        order_by="make, model")

    # Do NOT expose the supplier's real business name publicly — show
    # their marketplace code (the docname, e.g. "SUP-0001") instead, so
    # customers can't identify and go direct to the supplier.
    for v in vehicles:
        v["supplier_name"] = v.supplier or ""

    supplier_ids = list({v.supplier for v in vehicles if v.supplier})
    logo_map = {}
    if supplier_ids:
        logos = frappe.get_all(
            "Marketplace Supplier", filters={"name": ["in", supplier_ids]},
            fields=["name", "company_logo"])
        logo_map = {l.name: l.company_logo for l in logos if l.company_logo}
    for v in vehicles:
        v["supplier_logo"] = logo_map.get(v.supplier)

    if pickup_datetime and dropoff_datetime and vehicles:
        vehicle_ids = [v.name for v in vehicles]
        blocks = frappe.get_all(
            "Vehicle Availability Block",
            filters={"vehicle": ["in", vehicle_ids], "status": "Active",
                     "start_datetime": ["<", dropoff_datetime],
                     "end_datetime": [">", pickup_datetime]},
            fields=["vehicle", "start_datetime", "end_datetime"])
        bookings = frappe.get_all(
            "Booking",
            filters={"vehicle": ["in", vehicle_ids],
                     "booking_status": ["not in",
                                        ["Cancelled", "No Show", "Completed"]],
                     "pickup_datetime": ["<", dropoff_datetime],
                     "dropoff_datetime": [">", pickup_datetime]},
            fields=["vehicle", "pickup_datetime", "dropoff_datetime"])

        # Earliest conflicting window per vehicle, from either source
        conflicts = {}
        for b in blocks:
            row = (b.start_datetime, b.end_datetime)
            if b.vehicle not in conflicts or row[0] < conflicts[b.vehicle][0]:
                conflicts[b.vehicle] = row
        for bk in bookings:
            row = (bk.pickup_datetime, bk.dropoff_datetime)
            if bk.vehicle not in conflicts or row[0] < conflicts[bk.vehicle][0]:
                conflicts[bk.vehicle] = row

        for v in vehicles:
            if v.name in conflicts:
                v["available"] = False
                start, end = conflicts[v.name]
                v["unavailable_from"] = str(start)
                v["unavailable_until"] = str(end)
            else:
                v["available"] = True
    else:
        for v in vehicles:
            v["available"] = None

    classes = frappe.get_all(
        "Vehicle Class", filters={"active": 1},
        fields=["name", "class_name", "description"],
        order_by="sort_order")
    makes = sorted({v.make for v in vehicles if v.make})
    return {"vehicles": vehicles, "vehicle_classes": classes, "makes": makes}


# ── Supplier endpoints ─────────────────────────────────────────────────────────

@frappe.whitelist(allow_guest=False)
@api_handler
def submit_offer(tender_id, vehicle, discount=0, extras=None,
                 deposit_amount=0, cancellation_policy=None,
                 mileage_policy="", fuel_policy="", insurance_summary="",
                 supplier_notes="", idempotency_key=None):
    _require_login()
    supplier = perms.get_user_supplier(frappe.session.user)
    if not supplier:
        frappe.throw("No supplier linked to this user.",
                     frappe.PermissionError)

    def _do():
        offer = offer_service.submit_offer(
            tender_id, supplier, vehicle,
            discount=float(discount or 0),
            extras=json.loads(extras) if isinstance(extras, str) else extras,
            deposit_amount=float(deposit_amount or 0),
            cancellation_policy=cancellation_policy,
            mileage_policy=mileage_policy, fuel_policy=fuel_policy,
            insurance_summary=insurance_summary,
            supplier_notes=supplier_notes)
        return {"offer": offer.name, "total_amount": offer.total_amount,
                "valid_until": str(offer.valid_until)}

    return idempotency.run(idempotency_key, "submit_offer",
                           {"tender": tender_id, "vehicle": vehicle,
                            "supplier": supplier}, _do)


# ── Payment webhook (guest, signature-verified) ────────────────────────────────

@frappe.whitelist(allow_guest=True, methods=["POST"])
def payment_webhook(**kwargs):
    """Signature-verified, deduplicated, idempotent, queue-processed."""
    try:
        raw = frappe.request.get_data() or b"{}"
        signature = frappe.get_request_header("X-Marketplace-Signature") or ""
        payload = json.loads(raw or b"{}")
        valid = payment_service.verify_signature(raw, signature)
        event_name = payment_service.ingest_webhook(payload, valid) if valid else None
        if not valid:
            frappe.local.response["http_status_code"] = 401
            return {"success": False,
                    "error": {"code": "INVALID_SIGNATURE",
                              "message": "Signature verification failed."}}
        return {"success": True, "data": {"event": event_name}}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "payment_webhook")
        frappe.local.response["http_status_code"] = 500
        return {"success": False,
                "error": {"code": "INTERNAL_ERROR",
                          "message": "Webhook could not be stored."}}


# ── Operations endpoints (operator/finance gated) ──────────────────────────────

@frappe.whitelist(allow_guest=False)
@api_handler
def verify_supplier(supplier):
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin",
                                       "Car Marketplace Operations",
                                       "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)
    doc = frappe.get_doc("Marketplace Supplier", supplier)
    old = doc.status
    doc.status = "Verified"
    doc.save(ignore_permissions=True)
    audit.log("supplier_approved", "Marketplace Supplier", doc.name,
              old_state=old, new_state="Verified")

    # Sync a real ERPNext Supplier record automatically as soon as
    # this supplier is verified — no manual backfill needed going forward.
    from rental_marketplace.services import payment_service
    payment_service._get_or_create_supplier(doc.name)

    return {"supplier": doc.name, "status": doc.status}


@frappe.whitelist(allow_guest=False)
@api_handler
def suspend_supplier(supplier, reason=""):
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin",
                                       "Car Marketplace Operations",
                                       "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)
    doc = frappe.get_doc("Marketplace Supplier", supplier)
    old = doc.status
    doc.status = "Suspended"
    doc.save(ignore_permissions=True)
    audit.log("supplier_suspended", "Marketplace Supplier", doc.name,
              old_state=old, new_state="Suspended",
              metadata={"reason": reason})
    return {"supplier": doc.name, "status": doc.status}
@frappe.whitelist(allow_guest=False)
@api_handler
def reactivate_supplier(supplier):
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin",
                                       "Car Marketplace Operations",
                                       "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)
    doc = frappe.get_doc("Marketplace Supplier", supplier)
    old = doc.status
    doc.status = "Verified"
    doc.save(ignore_permissions=True)
    audit.log("supplier_reactivated", "Marketplace Supplier", doc.name,
              old_state=old, new_state="Verified")
    return {"supplier": doc.name, "status": doc.status}


@frappe.whitelist(allow_guest=False)
@api_handler
def approve_booking(booking):
    from rental_marketplace.services import booking_service
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin",
                                       "Car Marketplace Operations",
                                       "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)
    doc = booking_service.transition(booking, "approve")
    return {"booking": doc.name, "status": doc.booking_status}


@frappe.whitelist(allow_guest=False)
@api_handler
def reject_booking(booking, reason=""):
    from rental_marketplace.services import booking_service
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin",
                                       "Car Marketplace Operations",
                                       "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)
    doc = booking_service.transition(booking, "reject", metadata={"reason": reason})
    return {"booking": doc.name, "status": doc.booking_status}


@frappe.whitelist(allow_guest=False)
@api_handler
def create_supplier_login(supplier, email, password, role_level="Supplier Admin"):
    """Creates (or reuses) a User with the given email + password and
    links them into this Marketplace Supplier's portal, so they can log
    in to the supplier portal immediately with those credentials."""
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin",
                                       "Car Marketplace Operations",
                                       "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)

    email = (email or "").strip().lower()
    if not email or not password:
        frappe.throw("Email and password are required.", frappe.ValidationError)
    if len(password) < 10:
        frappe.throw("Password must be at least 10 characters.")

    sup = frappe.get_doc("Marketplace Supplier", supplier)

    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
        privileged = {"System Manager", "Car Marketplace Admin", "Car Marketplace Operations",
                      "Car Marketplace Finance", "Administrator"} & set(frappe.get_roles(user.name))
        linked_here = any(r.user == user.name for r in sup.portal_users)
        if user.user_type == "System User" or privileged or not linked_here:
            frappe.throw("That email already belongs to another account. Use a "
                         "different email, or ask the user to reset their own password.",
                         frappe.ValidationError)
    else:
        name_source = sup.legal_name or sup.supplier_name or email
        user = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": name_source.split(" ")[0],
            "last_name": " ".join(name_source.split(" ")[1:]) or "",
            "send_welcome_email": 0,
            "user_type": "Website User",
        })
        user.insert(ignore_permissions=True)

    from frappe.utils.password import update_password
    update_password(user.name, password)

    if role_level not in {r.role_level for r in sup.portal_users
                          if r.user == user.name}:
        already_linked = any(r.user == user.name for r in sup.portal_users)
        if not already_linked:
            sup.append("portal_users", {"user": user.name,
                                        "role_level": role_level})
            sup.save(ignore_permissions=True)

    if role_level not in frappe.get_roles(user.name):
        user.add_roles(role_level)

    frappe.db.commit()
    audit.log("supplier_login_created", "Marketplace Supplier", sup.name,
              metadata={"user": user.name, "role_level": role_level})
    return {"supplier": sup.name, "user": user.name, "role_level": role_level}


@frappe.whitelist(allow_guest=False)
@api_handler
def mark_booking_paid(booking, payment_reference=None):
    from rental_marketplace.services import payment_service
    return payment_service.mark_booking_paid(booking, payment_reference)


@frappe.whitelist(allow_guest=False)
@api_handler
def approve_settlement(settlement):
    if not (set(frappe.get_roles()) & {"Car Marketplace Finance",
                                       "Car Marketplace Admin",
                                       "System Manager"}):
        frappe.throw("Finance only.", frappe.PermissionError)
    doc = frappe.get_doc("Supplier Settlement", settlement)
    doc.status = "Approved"
    doc.save(ignore_permissions=True)  # controller enforces flow + role
    audit.log("settlement_approved", "Supplier Settlement", doc.name,
              new_state="Approved")
    return {"settlement": doc.name, "status": doc.status}


@frappe.whitelist(allow_guest=False)
@api_handler
def supplier_transition(booking_id, action):
    """Supplier-driven lifecycle actions (confirm/ready/pickup/activate/
    return/complete). Role + supplier ownership enforced; the state
    machine enforces allowed source states."""
    _require_login()
    supplier = perms.get_user_supplier(frappe.session.user)
    booking = frappe.get_doc("Booking", booking_id)
    if not supplier or booking.supplier != supplier:
        if not perms.is_operator(frappe.session.user):
            raise BookingNotFound()
    allowed = {"supplier_confirm", "ready", "pickup", "activate",
               "return", "complete", "no_show"}
    if action not in allowed:
        frappe.throw("Action not available to suppliers.",
                     frappe.PermissionError)
    booking = booking_service.transition(booking, action)
    return {"booking": booking.name, "booking_status": booking.booking_status}


@frappe.whitelist(allow_guest=False)
@api_handler
def partner_summary():
    """Partner portal data — only the partner's own attribution."""
    _require_login()
    partner = frappe.db.get_value(
        "Marketplace Partner", {"user": frappe.session.user},
        ["name", "partner_name", "partner_code", "status",
         "commission_share_percentage"], as_dict=True)
    if not partner:
        frappe.throw("No partner account linked to this user.",
                     frappe.PermissionError)
    bookings = frappe.get_all(
        "Booking", filters={"partner": partner.name},
        fields=["name", "booking_status", "gross_amount",
                "partner_commission", "currency", "creation"],
        order_by="creation desc", limit_page_length=50)
    earned = sum(b.partner_commission or 0 for b in bookings
                 if b.booking_status == "Completed")
    pipeline = sum(b.partner_commission or 0 for b in bookings
                   if b.booking_status in ("Confirmed", "Supplier Confirmed",
                                           "Ready for Pickup", "Picked Up",
                                           "Active Rental", "Returned"))
    requests_count = frappe.db.count("Car Rental Request",
                                     {"partner": partner.name})
    return {"partner": partner, "bookings": bookings,
            "earned": round(earned, 2), "pipeline": round(pipeline, 2),
            "requests_attributed": requests_count}


@frappe.whitelist(allow_guest=False)
@api_handler
def create_vehicle(**kwargs):
    _require_login()
    supplier = perms.get_user_supplier(frappe.session.user)
    if not supplier:
        frappe.throw("No supplier linked to this user.", frappe.PermissionError)
    fields = ("display_name", "vehicle_class", "make", "model", "model_year",
              "transmission", "fuel_type", "seats", "home_location",
              "daily_rate", "weekly_rate", "monthly_rate")
    doc = frappe.new_doc("Marketplace Vehicle")
    doc.supplier = supplier
    doc.active = 1
    for f in fields:
        if kwargs.get(f) not in (None, ""):
            doc.set(f, kwargs[f])
    doc.insert(ignore_permissions=True)
    return {"vehicle": doc.name}


@frappe.whitelist(allow_guest=False)
@api_handler
def update_vehicle_rates(vehicle, daily_rate=None, weekly_rate=None, monthly_rate=None):
    _require_login()
    supplier = perms.get_user_supplier(frappe.session.user)
    if not supplier:
        frappe.throw("No supplier linked to this user.", frappe.PermissionError)
    doc = frappe.get_doc("Marketplace Vehicle", vehicle)
    if doc.supplier != supplier:
        frappe.throw("Not your vehicle.", frappe.PermissionError)
    doc.daily_rate = daily_rate or 0
    doc.weekly_rate = weekly_rate or 0
    doc.monthly_rate = monthly_rate or 0
    doc.save(ignore_permissions=True)
    return {"vehicle": doc.name, "daily_rate": doc.daily_rate,
            "weekly_rate": doc.weekly_rate, "monthly_rate": doc.monthly_rate}


@frappe.whitelist(allow_guest=False)
@api_handler
def set_vehicle_photo(vehicle, file_url):
    _require_login()
    supplier = perms.get_user_supplier(frappe.session.user)
    owner = frappe.db.get_value("Marketplace Vehicle", vehicle, "supplier")
    if not supplier or owner != supplier:
        frappe.throw("Vehicle does not belong to this supplier.",
                     frappe.PermissionError)
    frappe.db.set_value("Marketplace Vehicle", vehicle, "primary_photo",
                        file_url)
    return {"vehicle": vehicle, "primary_photo": file_url}


@frappe.whitelist(allow_guest=False)
def download_invoice(booking_id, download=0):
    _require_login()
    booking = frappe.get_doc("Booking", booking_id)
    supplier = perms.get_user_supplier(frappe.session.user)
    if booking.customer != frappe.session.user and \
            booking.supplier != supplier and \
            not perms.is_operator(frappe.session.user):
        frappe.throw("Not permitted.", frappe.PermissionError)
    if booking.payment_status == "Unpaid":
        frappe.throw("Invoice is only available once payment has been made.")
    settings = frappe.get_single("Car Marketplace Settings")
    extras_lines = json.loads(booking.extras_snapshot or "[]")
    from frappe.utils import date_diff
    rental_days = max(date_diff(booking.dropoff_datetime,
                                booking.pickup_datetime), 1)
    tax_percentage_applied = None
    try:
        pricing_snapshot = json.loads(booking.pricing_snapshot or "{}")
        tax_snapshot = pricing_snapshot.get("tax_snapshot") or {}
        tax_percentage_applied = (tax_snapshot.get("tax_percentage")
                                  or settings.default_tax_percentage)
    except Exception:
        tax_percentage_applied = settings.default_tax_percentage
    html = frappe.render_template(
        "rental_marketplace/templates/includes/invoice.html",
        {"booking": booking, "settings": settings, "extras_lines": extras_lines,
         "rental_days": rental_days,
         "tax_percentage_applied": tax_percentage_applied})
    from frappe.utils.pdf import get_pdf
    pdf = get_pdf(html)
    frappe.local.response.filename = f"Invoice-{booking.name}.pdf"
    frappe.local.response.filecontent = pdf
    frappe.local.response.type = "download" if int(download or 0) else "pdf"


@frappe.whitelist(allow_guest=False)
@api_handler
def list_booking_extras(booking_id):
    """Extras available to add to this booking, plus what's already
    on it. Available = marketplace-wide (blank supplier) or specific
    to this booking's supplier."""
    _require_login()
    booking = frappe.get_doc("Booking", booking_id)
    if booking.customer != frappe.session.user:
        frappe.throw("Not your booking.", frappe.PermissionError)

    extras = frappe.get_all(
        "Marketplace Extra",
        filters={"active": 1},
        or_filters=[["supplier", "=", booking.supplier],
                    ["supplier", "in", ["", None]]],
        fields=["name", "extra_name", "category", "description",
                "pricing_method", "price", "currency"])

    added = json.loads(booking.extras_snapshot or "[]")
    return {"available": extras, "added": added,
            "extras_amount": booking.extras_amount or 0}


@frappe.whitelist(allow_guest=False)
@api_handler
def add_booking_extras(booking_id, extras, idempotency_key=None):
    """extras: JSON list of {"extra": <Marketplace Extra name>,
    "quantity": int}. Recomputes extras_amount/tax/commission/gross
    the same way pricing_service.compute() would, using this
    booking's applicable Car Pricing Rule (or supplier/settings
    fallbacks), and folds the delta into the booking."""
    _require_login()

    def _do():
        from decimal import Decimal
        from rental_marketplace.services import pricing_service, tax_service
        from frappe.utils import date_diff

        booking = frappe.get_doc("Booking", booking_id)
        if booking.customer != frappe.session.user:
            frappe.throw("Not your booking.", frappe.PermissionError)
        if booking.payment_status == "Paid":
            frappe.throw("This booking is already fully paid; extras "
                         "can no longer be added.")

        requested = json.loads(extras) if isinstance(extras, str) else extras
        if not requested:
            frappe.throw("No extras selected.")

        extra_ids = [r["extra"] for r in requested]
        catalog = {e.name: e for e in frappe.get_all(
            "Marketplace Extra", filters={"name": ["in", extra_ids]},
            fields=["name", "extra_name", "pricing_method", "price",
                    "currency"])}

        vehicle_class = frappe.db.get_value(
            "Marketplace Vehicle", booking.vehicle, "vehicle_class")
        rental_days = max(date_diff(booking.dropoff_datetime,
                                    booking.pickup_datetime), 1)

        rows = []
        for r in requested:
            e = catalog.get(r["extra"])
            if not e:
                continue
            rows.append({"extra": e.name, "extra_name": e.extra_name,
                        "pricing_method": e.pricing_method,
                        "price": e.price, "quantity": int(r.get("quantity") or 1)})

        extras_total, extra_lines = pricing_service.compute_extras(
            rows, rental_days, booking.base_amount or 0)
        if extras_total <= 0:
            frappe.throw("Selected extras total to zero.")

        settings = frappe.get_single("Car Marketplace Settings").as_dict()
        rules = frappe.get_all(
            "Car Pricing Rule",
            filters={"supplier": booking.supplier,
                     "vehicle_class": vehicle_class, "active": 1},
            fields=["name", "supplier", "vehicle_class", "location",
                    "country", "start_date", "end_date", "minimum_days",
                    "maximum_days", "commission_percentage",
                    "fixed_commission", "tax_percentage", "priority"])
        rule = pricing_service.select_rule(rules, {
            "supplier": booking.supplier, "vehicle_class": vehicle_class,
            "pickup_location": booking.pickup_location,
            "rental_days": rental_days,
            "pickup_date": booking.pickup_datetime}) or {}

        sup_comm = frappe.db.get_value(
            "Marketplace Supplier", booking.supplier, "commission_percentage")
        commission_pct = rule.get("commission_percentage")
        if commission_pct in (None, ""):
            commission_pct = sup_comm
        if commission_pct in (None, ""):
            commission_pct = settings.get("default_commission_percentage") or 0

        tax = tax_service.calculate(float(extras_total), {
            "tax_percentage": rule.get("tax_percentage"),
            "default_tax_percentage": settings.get("default_tax_percentage")})
        extra_tax = pricing_service.q2(tax["total_tax"])
        extra_commission = pricing_service.q2(
            Decimal(str(extras_total)) * Decimal("15") / Decimal("100"))
        extra_commission += pricing_service.q2(rule.get("fixed_commission"))
        extra_gross = pricing_service.q2(Decimal(str(extras_total)) + extra_tax)
        extra_supplier_share = pricing_service.q2(
            extra_gross - extra_tax - extra_commission)

        existing = json.loads(booking.extras_snapshot or "[]")
        by_extra = {row["extra"]: row for row in existing}
        for row in extra_lines:
            key = row["extra"]
            if key in by_extra:
                by_extra[key]["quantity"] += row["quantity"]
                by_extra[key]["amount"] += row["amount"]
            else:
                by_extra[key] = dict(row)
        merged = list(by_extra.values())

        booking.db_set("extras_snapshot", json.dumps(merged, default=str))
        booking.db_set("extras_amount", (booking.extras_amount or 0) + float(extras_total))
        booking.db_set("tax_amount", (booking.tax_amount or 0) + float(extra_tax))
        booking.db_set("commission_amount", (booking.commission_amount or 0) + float(extra_commission))
        booking.db_set("gross_amount", (booking.gross_amount or 0) + float(extra_gross))
        booking.db_set("supplier_amount", (booking.supplier_amount or 0) + float(extra_supplier_share))
        booking.db_set("amount_due",
                       (booking.commission_amount or 0)
                       + (booking.tax_amount or 0)
                       - (booking.amount_paid or 0))

        audit.log("booking_extras_added", "Booking", booking.name,
                  metadata={"extras": merged, "added_total": float(extras_total)})

        booking.reload()
        return {"booking": booking.name,
                "extras_amount": booking.extras_amount,
                "gross_amount": booking.gross_amount,
                "supplier_amount": booking.supplier_amount,
                "amount_due": booking.amount_due}

    return idempotency.run(idempotency_key, "add_booking_extras",
                           {"booking": booking_id, "extras": extras}, _do)


@frappe.whitelist(allow_guest=False)
@api_handler
def remove_booking_extra(booking_id, extra, idempotency_key=None):
    """Reverses one previously-added extra line, using the same
    commission/tax percentages add_booking_extras would apply today,
    so the totals round-trip cleanly."""
    _require_login()

    def _do():
        from decimal import Decimal
        from rental_marketplace.services import pricing_service, tax_service
        from frappe.utils import date_diff

        booking = frappe.get_doc("Booking", booking_id)
        if booking.customer != frappe.session.user:
            frappe.throw("Not your booking.", frappe.PermissionError)
        if booking.payment_status == "Paid":
            frappe.throw("This booking is already fully paid; extras "
                         "can no longer be changed.")

        existing = json.loads(booking.extras_snapshot or "[]")
        row = next((r for r in existing if r["extra"] == extra), None)
        if not row:
            frappe.throw("That extra is not on this booking.")

        removed_amount = Decimal(str(row["amount"]))

        vehicle_class = frappe.db.get_value(
            "Marketplace Vehicle", booking.vehicle, "vehicle_class")
        rental_days = max(date_diff(booking.dropoff_datetime,
                                    booking.pickup_datetime), 1)
        settings = frappe.get_single("Car Marketplace Settings").as_dict()
        rules = frappe.get_all(
            "Car Pricing Rule",
            filters={"supplier": booking.supplier,
                     "vehicle_class": vehicle_class, "active": 1},
            fields=["name", "supplier", "vehicle_class", "location",
                    "country", "start_date", "end_date", "minimum_days",
                    "maximum_days", "commission_percentage",
                    "fixed_commission", "tax_percentage", "priority"])
        rule = pricing_service.select_rule(rules, {
            "supplier": booking.supplier, "vehicle_class": vehicle_class,
            "pickup_location": booking.pickup_location,
            "rental_days": rental_days,
            "pickup_date": booking.pickup_datetime}) or {}

        sup_comm = frappe.db.get_value(
            "Marketplace Supplier", booking.supplier, "commission_percentage")
        commission_pct = rule.get("commission_percentage")
        if commission_pct in (None, ""):
            commission_pct = sup_comm
        if commission_pct in (None, ""):
            commission_pct = settings.get("default_commission_percentage") or 0

        tax = tax_service.calculate(float(removed_amount), {
            "tax_percentage": rule.get("tax_percentage"),
            "default_tax_percentage": settings.get("default_tax_percentage")})
        removed_tax = pricing_service.q2(tax["total_tax"])
        removed_commission = pricing_service.q2(
            removed_amount * Decimal("15") / Decimal("100"))
        removed_commission += pricing_service.q2(rule.get("fixed_commission"))
        removed_gross = pricing_service.q2(removed_amount + removed_tax)
        removed_supplier_share = pricing_service.q2(
            removed_gross - removed_tax - removed_commission)

        remaining = [r for r in existing if r["extra"] != extra]
        booking.db_set("extras_snapshot", json.dumps(remaining, default=str))
        booking.db_set("extras_amount", max(0, (booking.extras_amount or 0) - float(removed_amount)))
        booking.db_set("tax_amount", max(0, (booking.tax_amount or 0) - float(removed_tax)))
        booking.db_set("commission_amount", max(0, (booking.commission_amount or 0) - float(removed_commission)))
        booking.db_set("gross_amount", max(0, (booking.gross_amount or 0) - float(removed_gross)))
        booking.db_set("supplier_amount", max(0, (booking.supplier_amount or 0) - float(removed_supplier_share)))
        booking.db_set("amount_due", max(0,
                       (booking.commission_amount or 0)
                       + (booking.tax_amount or 0)
                       - (booking.amount_paid or 0)))

        audit.log("booking_extra_removed", "Booking", booking.name,
                  metadata={"extra": extra, "removed_amount": float(removed_amount)})

        booking.reload()
        return {"booking": booking.name,
                "extras_amount": booking.extras_amount,
                "gross_amount": booking.gross_amount,
                "supplier_amount": booking.supplier_amount,
                "amount_due": booking.amount_due}

    return idempotency.run(idempotency_key, "remove_booking_extra",
                           {"booking": booking_id, "extra": extra}, _do)


_LOCATION_TYPES = ("Airport", "City", "Port", "Hotel Zone", "Other")


def _require_operator_role():
    if not (set(frappe.get_roles()) & {"Car Marketplace Admin", "Car Marketplace Operations", "System Manager"}):
        frappe.throw("Operators only.", frappe.PermissionError)


@frappe.whitelist(allow_guest=False)
@api_handler
def add_location(location_name, location_type="City", city="", country=""):
    """Create a pick-up / drop-off location from the Operations console."""
    import re as _re
    _require_operator_role()
    location_name = (location_name or "").strip()
    if not location_name or len(location_name) > 80:
        frappe.throw("Enter a location name (up to 80 characters).", frappe.ValidationError)
    if location_type not in _LOCATION_TYPES:
        frappe.throw("Choose a valid location type.", frappe.ValidationError)
    if frappe.db.exists("Marketplace Location", {"location_name": location_name,
                                                 "location_type": location_type}):
        frappe.throw("This location already exists.", frappe.ValidationError)
    slug = lambda t: _re.sub(r"[^A-Z0-9]+", "-", (t or "").upper()).strip("-")
    base = slug(location_name)[:24] or "LOC"
    if frappe.db.exists("Marketplace Location", base):
        base = base[:20].strip("-") + "-" + slug(location_type)[:5]
    code, n = base, 1
    while frappe.db.exists("Marketplace Location", code):
        n += 1
        code = f"{base[:26]}-{n}"
    doc = frappe.get_doc({"doctype": "Marketplace Location", "location_code": code,
                          "location_name": location_name, "location_type": location_type,
                          "city": (city or "").strip()[:60], "country": (country or "").strip()[:60],
                          "active": 1})
    doc.insert(ignore_permissions=True)
    return {"location": doc.name}


@frappe.whitelist(allow_guest=False)
@api_handler
def set_location_active(location, active):
    """Show or hide a location in the website drop-downs."""
    _require_operator_role()
    if not frappe.db.exists("Marketplace Location", location):
        frappe.throw("Location not found.", frappe.ValidationError)
    frappe.db.set_value("Marketplace Location", location, "active", 1 if int(active) else 0)
    return {"location": location, "active": 1 if int(active) else 0}

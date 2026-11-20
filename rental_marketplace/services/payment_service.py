"""PaymentService — intents, webhook ingestion, ledger updates.

No payment gateway is wired in yet. Flow used instead:
  1. create_payment_intent() — called when the customer reaches the
     payment page / picks a payment method. Creates the Sales Invoice
     right here, as a DRAFT, renamed to match the booking's own
     reference so "our invoice number" and the Sales Invoice number
     are identical. Extras are included as their own line items.
  2. Booking stays payment_status="Unpaid", Sales Invoice stays Draft.
  3. An operator/finance user confirms payment happened offline and
     clicks "Mark Paid" on /operations, which calls mark_booking_paid()
     below — that SUBMITS the Sales Invoice, creates a Payment Entry
     against it, and moves the booking to Confirmed/Paid.

Webhook flow (spec, for when a real gateway is added): verify
signature → store raw event → dedupe → enqueue processing → update
payment → update booking → ledger → notify. Deliveries must be safe
to repeat. If a webhook succeeds, it submits the existing draft
Sales Invoice (created at intent time) rather than creating a new one.
"""

import hashlib
import hmac
import json

import frappe
from frappe.utils import now_datetime, add_to_date, today

from rental_marketplace.services import audit, notification_service
from rental_marketplace.exceptions import (
    BookingNotFound, PaymentAlreadyProcessed)

OPERATOR_ROLES = {"Car Marketplace Admin", "Car Marketplace Operations",
                  "Car Marketplace Finance", "System Manager"}


def create_payment_intent(booking_name, customer_user, full_payment=False):
    """Amount always comes from the booking (server-side) — never
    from the client. full_payment=True charges the full rental amount
    (platform fee + vendor's share) instead of just the platform fee.

    Also creates the Draft Sales Invoice at this point (once), so it
    exists as soon as the customer reaches the payment step."""
    booking = frappe.get_doc("Booking", booking_name)
    if booking.customer != customer_user and \
            "Car Marketplace Admin" not in frappe.get_roles():
        frappe.throw("Not your booking.", frappe.PermissionError)

    if full_payment:
        target_amount = round(
            (booking.gross_amount or 0) - (booking.amount_paid or 0), 2)
    else:
        target_amount = booking.amount_due

    if target_amount <= 0:
        raise PaymentAlreadyProcessed("Nothing due on this booking.")

    frappe.db.set_value("Booking", booking.name,
                        "collect_full_amount", 1 if full_payment else 0)
    booking.reload()

    if not booking.sales_invoice:
        si_name = _create_draft_sales_invoice(booking, full_payment)
        booking.db_set("sales_invoice", si_name)

    intent = frappe.new_doc("Payment Intent")
    intent.booking = booking.name
    intent.customer = booking.customer
    intent.amount = target_amount
    intent.currency = booking.currency
    intent.provider = "stub"
    intent.provider_intent_id = f"pi_{frappe.generate_hash(length=20)}"
    intent.status = "Created"
    intent.created_at = now_datetime()
    intent.expires_at = add_to_date(now_datetime(), minutes=60)
    intent.insert(ignore_permissions=True)
    audit.log("payment_intent_created", "Payment Intent", intent.name,
              metadata={"booking": booking.name, "amount": intent.amount})
    return intent


def _get_or_create_customer(booking):
    """Real ERPNext Customer from the booking's Customer Profile,
    instead of the generic 'Marketplace Guest Customer' placeholder.
    Reused across bookings for the same user once created."""
    profile = frappe.db.get_value(
        "Customer Profile", {"user": booking.customer},
        ["first_name", "last_name", "phone"], as_dict=True) or {}
    full_name = " ".join(filter(None, [profile.get("first_name"),
                                       profile.get("last_name")])).strip()
    if not full_name:
        full_name = booking.customer  # fallback: their login email

    existing = frappe.db.get_value("Customer", {"customer_name": full_name})
    if existing:
        return existing

    customer_group = frappe.db.get_single_value(
        "Selling Settings", "customer_group") or "Individual"
    if not frappe.db.exists("Customer Group", customer_group):
        customer_group = "All Customer Groups"
    territory = frappe.db.get_single_value(
        "Selling Settings", "territory") or "All Territories"
    if not frappe.db.exists("Territory", territory):
        territory = frappe.db.get_all("Territory", limit=1, pluck="name")
        territory = territory[0] if territory else None

    cust = frappe.new_doc("Customer")
    cust.customer_name = full_name
    cust.customer_type = "Individual"
    cust.customer_group = customer_group
    if territory:
        cust.territory = territory
    if profile.get("phone"):
        cust.mobile_no = profile.get("phone")
    cust.flags.ignore_permissions = True
    cust.insert(ignore_permissions=True)
    return cust.name


def _get_or_create_supplier(supplier_code):
    """Real ERPNext Supplier from the Marketplace Supplier — renamed to
    match the same code (e.g. S-1005) so the two records stay identifiable
    as the same supplier across the marketplace app and core accounting."""
    if not supplier_code:
        return None
    if frappe.db.exists("Supplier", supplier_code):
        return supplier_code

    sup = frappe.db.get_value(
        "Marketplace Supplier", supplier_code,
        ["supplier_name", "legal_name", "contact_email", "contact_phone"],
        as_dict=True)
    if not sup:
        return None
    display_name = sup.legal_name or sup.supplier_name

    # An existing Supplier from before this fix might exist under the
    # business name instead of the code — bring it in line by renaming it.
    existing = frappe.db.get_value("Supplier", {"supplier_name": display_name})
    if existing and existing != supplier_code:
        from frappe.model.rename_doc import rename_doc as _rename_doc
        _rename_doc("Supplier", existing, supplier_code,
                   force=True, ignore_permissions=True)
        # Renaming (Supplier is auto-named from supplier_name) overwrites
        # supplier_name to match the new code — restore the real name.
        frappe.db.set_value("Supplier", supplier_code, "supplier_name",
                            display_name)
        return supplier_code

    supplier_group = frappe.db.get_single_value(
        "Buying Settings", "supplier_group")
    if not supplier_group or not frappe.db.exists("Supplier Group", supplier_group):
        supplier_group = frappe.db.get_all("Supplier Group", limit=1, pluck="name")
        supplier_group = supplier_group[0] if supplier_group else None

    s = frappe.new_doc("Supplier")
    s.supplier_name = display_name
    s.supplier_type = "Company"
    if supplier_group:
        s.supplier_group = supplier_group
    if sup.contact_email:
        s.email_id = sup.contact_email
    if sup.contact_phone:
        s.mobile_no = sup.contact_phone
    s.flags.ignore_permissions = True
    s.insert(ignore_permissions=True)

    if s.name != supplier_code and not frappe.db.exists("Supplier", supplier_code):
        from frappe.model.rename_doc import rename_doc as _rename_doc
        _rename_doc("Supplier", s.name, supplier_code,
                   force=True, ignore_permissions=True)
        # Same auto-name side effect as above — restore the real name.
        frappe.db.set_value("Supplier", supplier_code, "supplier_name",
                            display_name)
        return supplier_code
    return s.name


def renumber_suppliers_to_s_series():
    """One-off: renames existing SUP-xxxx suppliers to the new S-1000+
    format the manager wants, and sets the naming series counter so
    future auto-generated codes continue after the last one used."""
    from frappe.model.rename_doc import rename_doc as _rename_doc

    codes = frappe.get_all("Marketplace Supplier", order_by="creation asc",
                           pluck="name")
    next_num = 1000
    renamed = {}
    for old_code in codes:
        if old_code.startswith("S-") and old_code[2:].isdigit():
            continue  # already renamed
        new_code = f"S-{next_num}"
        while frappe.db.exists("Marketplace Supplier", new_code):
            next_num += 1
            new_code = f"S-{next_num}"
        _rename_doc("Marketplace Supplier", old_code, new_code,
                   force=True, ignore_permissions=True)
        renamed[old_code] = new_code
        next_num += 1

    # Point the "S-" naming series counter past the highest number used,
    # so the next auto-generated supplier doesn't collide.
    highest = max(
        [int(c.split("-")[1]) for c in frappe.get_all(
            "Marketplace Supplier", pluck="name")
         if c.startswith("S-") and c.split("-")[1].isdigit()],
        default=999)
    series_key = "S-"
    existing_series = frappe.db.sql(
        "select name from `tabSeries` where name=%s", series_key)
    if existing_series:
        frappe.db.sql("update `tabSeries` set current=%s where name=%s",
                      (highest, series_key))
    else:
        frappe.db.sql("insert into `tabSeries` (name, current) values (%s, %s)",
                      (series_key, highest))

    frappe.db.commit()
    result = {"renamed": renamed, "next_will_be": f"S-{highest + 1}"}
    print(result)
    return result


def fix_supplier_names_and_orphans():
    """Fixes two side-effects: (1) renaming a Supplier can overwrite its
    supplier_name field to match the new code — restore the real business
    name. (2) any orphaned Supplier left over from before a Marketplace
    Supplier was renamed to its S-xxxx code — merge/rename it into place."""
    from frappe.model.rename_doc import rename_doc as _rename_doc

    fixed_names, merged_orphans = [], []

    for code in frappe.get_all("Marketplace Supplier", pluck="name"):
        ms = frappe.db.get_value(
            "Marketplace Supplier", code,
            ["supplier_name", "legal_name"], as_dict=True)
        correct_name = ms.legal_name or ms.supplier_name
        if not correct_name:
            continue
        if frappe.db.exists("Supplier", code):
            current = frappe.db.get_value("Supplier", code, "supplier_name")
            if current != correct_name:
                frappe.db.set_value("Supplier", code, "supplier_name",
                                    correct_name)
                fixed_names.append(code)

    # Orphans: Supplier records whose name isn't any current Marketplace
    # Supplier code (leftovers from before a rename caught up with them).
    valid_codes = set(frappe.get_all("Marketplace Supplier", pluck="name"))
    for s_name, s_display in frappe.get_all(
            "Supplier", fields=["name", "supplier_name"], as_list=False,
            filters={}, order_by="name"):
        pass
    all_suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"])
    for s in all_suppliers:
        if s.name in valid_codes:
            continue
        match = frappe.db.get_value(
            "Marketplace Supplier",
            {"supplier_name": s.supplier_name}, "name") or \
            frappe.db.get_value(
            "Marketplace Supplier", {"legal_name": s.supplier_name}, "name")
        if match and not frappe.db.exists("Supplier", match):
            _rename_doc("Supplier", s.name, match,
                       force=True, ignore_permissions=True)
            correct_name = frappe.db.get_value(
                "Marketplace Supplier", match, "legal_name") or \
                frappe.db.get_value("Marketplace Supplier", match, "supplier_name")
            if correct_name:
                frappe.db.set_value("Supplier", match, "supplier_name",
                                    correct_name)
            merged_orphans.append({"was": s.name, "now": match})

    frappe.db.commit()
    result = {"fixed_names": fixed_names, "merged_orphans": merged_orphans}
    print(result)
    return result


def fix_stray_orphan(orphan_supplier_name, real_marketplace_code):
    """One-off manual fix: merges a stray Supplier (created before its
    Marketplace Supplier was renamed) into the correct S-xxxx code,
    restoring the real business name."""
    from frappe.model.rename_doc import rename_doc as _rename_doc

    correct_name = frappe.db.get_value(
        "Marketplace Supplier", real_marketplace_code, "legal_name") or \
        frappe.db.get_value(
        "Marketplace Supplier", real_marketplace_code, "supplier_name")

    if frappe.db.exists("Supplier", real_marketplace_code):
        # A (wrongly-named) Supplier already sits at the target code —
        # remove it so the orphan can take its place.
        frappe.delete_doc("Supplier", real_marketplace_code,
                          force=True, ignore_permissions=True)

    _rename_doc("Supplier", orphan_supplier_name, real_marketplace_code,
               force=True, ignore_permissions=True)
    if correct_name:
        frappe.db.set_value("Supplier", real_marketplace_code,
                            "supplier_name", correct_name)
    frappe.db.commit()
    result = {"merged": orphan_supplier_name, "into": real_marketplace_code,
             "name_set_to": correct_name}
    print(result)
    return result


def backfill_all_suppliers():
    """Syncs an ERPNext Supplier for every Marketplace Supplier on file,
    not just ones that already have a booking with an invoice."""
    created = []
    for code in frappe.get_all("Marketplace Supplier", pluck="name"):
        before = frappe.db.count("Supplier")
        _get_or_create_supplier(code)
        after = frappe.db.count("Supplier")
        if after > before:
            created.append(code)
    result = {"synced_for": frappe.get_all("Marketplace Supplier", pluck="name"),
             "newly_created": created}
    print(result)
    return result


def backfill_customers_suppliers():
    """One-off: fix existing bookings/Sales Invoices that were created
    before real Customer/Supplier records existed — replaces the
    placeholder customer and links the right Supplier. Draft invoices
    are updated directly; anything already submitted is left alone
    since submitted docs can't be edited (should be none currently,
    since we never submit Sales Invoices)."""
    fixed, skipped = [], []
    bookings = frappe.get_all(
        "Booking", filters={"sales_invoice": ["!=", ""]},
        fields=["name", "customer", "supplier", "sales_invoice"])
    for b in bookings:
        si_name = b.sales_invoice
        if not frappe.db.exists("Sales Invoice", si_name):
            continue
        si = frappe.get_doc("Sales Invoice", si_name)
        if si.docstatus != 0:
            skipped.append(si_name)
            continue
        correct_customer = _get_or_create_customer(
            frappe._dict({"customer": b.customer}))
        _get_or_create_supplier(b.supplier)
        if si.customer != correct_customer:
            si.customer = correct_customer
            si.flags.ignore_permissions = True
            si.save(ignore_permissions=True)
            fixed.append(si_name)
    result = {"fixed": fixed, "skipped_submitted": skipped}
    print(result)
    return result


def _ensure_item(item_code, item_name):
    """Sales Invoice line items reference an Item master record —
    auto-create it (as a non-stock service item) if it doesn't exist
    yet, since these are platform-defined codes, not a real catalog."""
    if frappe.db.exists("Item", item_code):
        return
    item_group = "Services" if frappe.db.exists("Item Group", "Services")         else frappe.db.get_single_value("Stock Settings", "item_group")         or "All Item Groups"
    it = frappe.new_doc("Item")
    it.item_code = item_code
    it.item_name = item_name
    it.item_group = item_group
    it.is_stock_item = 0
    it.include_item_in_manufacturing = 0
    it.insert(ignore_permissions=True)


def _create_draft_sales_invoice(booking, full_payment):
    """Creates the Sales Invoice as a DRAFT — never submitted here.
    Renamed to match the booking's own reference so the invoice number
    the customer sees and the Sales Invoice number are the same.
    Extras always appear as their own line items."""
    extras_lines = json.loads(booking.extras_snapshot or "[]")
    rent_amount = round((booking.base_amount or 0) - (booking.discount_amount or 0), 2)
    fees_amount = round(booking.fees_amount or 0, 2)

    si = frappe.new_doc("Sales Invoice")
    si.customer = _get_or_create_customer(booking)
    _get_or_create_supplier(booking.supplier)  # kept on file for accounting
    si.company = _company()
    si.currency = frappe.db.get_value("Company", _company(), "default_currency")

    if full_payment:
        _ensure_item("RENTAL", "Vehicle Rental")
        si.append("items", {
            "item_code": "RENTAL",
            "item_name": "Vehicle Rental",
            "description": f"Rent (Vehicle: {booking.vehicle}, "
                           f"{frappe.utils.format_date(booking.pickup_datetime)} to "
                           f"{frappe.utils.format_date(booking.dropoff_datetime)})",
            "qty": 1, "rate": rent_amount,
            "income_account": "Service - K",
        })
        for e in extras_lines:
            _ensure_item("EXTRA", "Extra")
            si.append("items", {
                "item_code": "EXTRA",
                "item_name": e.get("extra_name") or e.get("extra") or "Extra",
                "description": e.get("extra_name") or e.get("extra") or "Extra",
                "qty": e.get("quantity") or 1,
                "rate": round(float(e.get("amount") or 0) /
                              max(int(e.get("quantity") or 1), 1), 2),
                "income_account": "Service - K",
            })
        if fees_amount:
            _ensure_item("FEES", "Delivery / One-way Fee")
            si.append("items", {
                "item_code": "FEES",
                "item_name": "Delivery / One-way Fee",
                "description": "Delivery / one-way fee",
                "qty": 1, "rate": fees_amount,
                "income_account": "Service - K",
            })
    else:
        fee_amount = round(booking.commission_amount or 0, 2)
        _ensure_item("SERVICE-FEE", "Service Fee")
        si.append("items", {
            "item_code": "SERVICE-FEE",
            "item_name": "Service Fee",
            "description": f"Service Fee (Vehicle Rental - {booking.vehicle}, "
                           f"{frappe.utils.format_date(booking.pickup_datetime)} to "
                           f"{frappe.utils.format_date(booking.dropoff_datetime)})",
            "qty": 1, "rate": fee_amount,
            "income_account": "Service - K",
        })
        for e in extras_lines:
            _ensure_item("EXTRA", "Extra")
            si.append("items", {
                "item_code": "EXTRA",
                "item_name": e.get("extra_name") or e.get("extra") or "Extra",
                "description": e.get("extra_name") or e.get("extra") or "Extra",
                "qty": e.get("quantity") or 1,
                "rate": round(float(e.get("amount") or 0) /
                              max(int(e.get("quantity") or 1), 1), 2),
                "income_account": "Service - K",
            })

    if booking.tax_amount:
        si.append("taxes", {
            "charge_type": "Actual",
            "account_head": "Service - K",
            "description": "VAT",
            "tax_amount": round(booking.tax_amount, 2),
        })
    si.insert(ignore_permissions=True)

    desired_name = booking.booking_reference or booking.name
    if si.name != desired_name and not frappe.db.exists("Sales Invoice", desired_name):
        from frappe.model.rename_doc import rename_doc as _rename_doc
        _rename_doc("Sales Invoice", si.name, desired_name,
                   force=True, ignore_permissions=True)
        si_name = desired_name
    else:
        si_name = si.name

    audit.log("sales_invoice_drafted", "Sales Invoice", si_name,
              metadata={"booking": booking.name})
    return si_name


@frappe.whitelist(allow_guest=False)
def mark_booking_paid(booking, payment_reference=None):
    """Manual replacement for a payment gateway: an operator/finance
    user confirms payment was received offline. Submits the Draft
    Sales Invoice, creates a Payment Entry against it, and moves the
    booking to Confirmed/Paid. Safe to call only once per booking."""
    from rental_marketplace.services import booking_service

    if not (set(frappe.get_roles()) & OPERATOR_ROLES):
        frappe.throw("Operators only.", frappe.PermissionError)

    frappe.db.get_value("Booking", booking, "name", for_update=True)  # row lock: no double payment
    doc = frappe.get_doc("Booking", booking)
    if doc.payment_status == "Paid":
        frappe.throw("This booking is already marked as paid.")
    if not doc.sales_invoice:
        frappe.throw("No Sales Invoice on this booking yet — the customer "
                     "hasn't reached the payment step.")

    si = frappe.get_doc("Sales Invoice", doc.sales_invoice)
    # Sales Invoice is intentionally left in Draft — always. It is
    # never submitted, here or anywhere else in this flow.

    if doc.collect_full_amount:
        platform_total = round(doc.gross_amount or 0, 2)
    else:
        platform_total = round((doc.commission_amount or 0)
                               + (doc.tax_amount or 0), 2)
    outstanding = round(platform_total - (doc.amount_paid or 0), 2)

    pe = None
    if outstanding > 0:
        # Standalone Payment Entry (not linked to the Sales Invoice) —
        # linking would require the invoice to be submitted, which we
        # deliberately never do. This still records the money received.
        default_account = frappe.db.get_value(
            "Company", _company(), "default_bank_account") or \
            frappe.db.get_value("Company", _company(), "default_cash_account")
        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Receive"
        pe.party_type = "Customer"
        pe.party = si.customer
        pe.company = _company()
        pe.paid_to = default_account
        pe.paid_amount = outstanding
        pe.received_amount = outstanding
        pe.source_exchange_rate = 1
        pe.target_exchange_rate = 1
        pe.reference_no = payment_reference or doc.name
        pe.reference_date = today()
        pe.remarks = f"Manual payment confirmation for booking {doc.name} " \
                     f"(Sales Invoice {si.name}, kept in Draft)"
        pe.flags.ignore_permissions = True
        pe.insert(ignore_permissions=True)
        pe.flags.ignore_permissions = True
        pe.submit()

    txn = frappe.new_doc("Payment Transaction")
    txn.booking = doc.name
    txn.transaction_type = "Charge"
    txn.status = "Succeeded"
    txn.amount = outstanding
    txn.currency = doc.currency
    txn.provider_transaction_id = payment_reference or f"manual_{doc.name}"
    txn.processed_at = now_datetime()
    txn.raw_reference = pe.name if pe else si.name
    txn.insert(ignore_permissions=True)

    doc.db_set("amount_paid", platform_total)
    doc.db_set("amount_due", 0)
    doc.reload()
    doc.db_set("payment_status", "Paid")
    if doc.booking_status == "Pending Payment":
        booking_service.transition(doc, "confirm", system=True,
                                   metadata={"txn": txn.name, "manual": True})

    audit.log("payment_marked_paid", "Booking", doc.name,
              metadata={"sales_invoice": si.name,
                       "payment_entry": pe.name if pe else None,
                       "amount": outstanding})
    notification_service.send("payment_received", doc.customer,
                              {"booking": doc.name})
    return {"booking": doc.name, "sales_invoice": si.name,
           "payment_entry": pe.name if pe else None,
           "status": doc.payment_status}


def start_paypal_checkout(booking_name, customer_user, full_payment=False):
    """Creates (or reuses) the Payment Intent for this booking, then
    opens a real PayPal order against it. Returns the approval URL the
    customer should be redirected to."""
    from rental_marketplace.services import paypal_gateway

    intent = create_payment_intent(booking_name, customer_user,
                                   full_payment=full_payment)
    order_id, approval_url = paypal_gateway.create_order(intent)
    intent.db_set("provider", "paypal")
    intent.db_set("provider_intent_id", order_id)
    audit.log("paypal_checkout_started", "Payment Intent", intent.name,
              metadata={"booking": booking_name, "order_id": order_id})
    return approval_url


def complete_paypal_payment(order_id):
    """Called when the customer returns from PayPal. Captures the
    order server-side (never trusts the redirect alone), and — only if
    PayPal confirms COMPLETED — feeds the existing webhook pipeline
    with the intent's native-currency amount, exactly like a real
    webhook would."""
    from rental_marketplace.services import paypal_gateway

    intent_name = frappe.db.get_value(
        "Payment Intent", {"provider_intent_id": order_id, "provider": "paypal"})
    if not intent_name:
        raise BookingNotFound("Unknown PayPal order.")
    intent = frappe.get_doc("Payment Intent", intent_name)

    if intent.status == "Succeeded":
        return {"already_processed": True, "booking": intent.booking}

    result = paypal_gateway.capture_order(order_id)
    status = result.get("status")
    if status != "COMPLETED":
        intent.db_set("status", "Failed")
        frappe.db.commit()
        frappe.throw(f"PayPal payment was not completed (status: {status}).")

    payload = {
        "event_id": f"paypal_{order_id}_{frappe.generate_hash(length=8)}",
        "type": "payment.succeeded",
        "provider_intent_id": order_id,
        "amount": intent.amount,          # native currency — matches booking ledger
        "booking": intent.booking,
        "transaction_id": order_id,
    }
    event_name = ingest_webhook(payload, signature_valid=True, provider="paypal",
                                 enqueue=False)
    process_webhook_event(event_name)
    # This runs from a plain GET request (the browser landing on
    # /paypal_return), which Frappe does not auto-commit at request end
    # the way it does for POST/API calls — without an explicit commit
    # here, everything above would be rolled back on connection close
    # and the payment would silently appear successful on-screen while
    # never actually being saved.
    frappe.db.commit()
    return {"already_processed": False, "booking": intent.booking}


def simulate_demo_payment(intent_name):
    """DEMO ONLY — kept for testing without a real gateway. Runs the
    same success path a real webhook would trigger."""
    intent = frappe.get_doc("Payment Intent", intent_name)
    payload = {
        "event_id": f"demo_{intent.name}_{frappe.generate_hash(length=8)}",
        "type": "payment.succeeded",
        "provider_intent_id": intent.provider_intent_id,
        "amount": intent.amount,
        "booking": intent.booking,
    }
    event_name = ingest_webhook(payload, signature_valid=True, provider="demo")
    process_webhook_event(event_name)
    return event_name


def verify_signature(raw_body, signature):
    secret = None
    try:
        secret = frappe.get_single("Car Marketplace Settings").get_password(
            "webhook_secret", raise_exception=False)
    except Exception:
        pass
    if not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def ingest_webhook(payload, signature_valid, provider="stub", enqueue=True):
    """Store raw event, dedupe on provider_event_id, enqueue processing.
    Returns the stored (or previously stored) event name.

    enqueue=False is used by callers (like the PayPal return flow) that
    are about to call process_webhook_event() themselves, synchronously,
    right after this returns — queuing it as well would process the
    same event twice (once inline, once via the background worker),
    which is redundant and can race against transaction commit
    visibility between the two."""
    event_id = payload.get("event_id") or payload.get("id")
    if not event_id:
        frappe.throw("Missing event_id.")

    existing = frappe.db.get_value("Payment Webhook Event",
                                   {"provider_event_id": event_id})
    if existing:
        return existing  # duplicate delivery — already handled/queued

    evt = frappe.new_doc("Payment Webhook Event")
    evt.provider = provider
    evt.provider_event_id = event_id
    evt.event_type = payload.get("type") or "payment.succeeded"
    evt.raw_payload = json.dumps(payload, default=str)
    evt.signature_valid = 1 if signature_valid else 0
    evt.processing_status = "Queued" if signature_valid else "Ignored"
    evt.received_at = now_datetime()
    try:
        evt.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        return frappe.db.get_value("Payment Webhook Event",
                                   {"provider_event_id": event_id})
    if signature_valid and enqueue:
        frappe.enqueue(
            "rental_marketplace.services.payment_service.process_webhook_event",
            queue="short", event_name=evt.name)
    return evt.name


def process_webhook_event(event_name):
    """Idempotent worker: repeated processing of the same event is a
    no-op after the first success."""
    evt = frappe.get_doc("Payment Webhook Event", event_name)
    if evt.processing_status == "Processed":
        return
    payload = json.loads(evt.raw_payload or "{}")
    try:
        if evt.event_type in ("payment.succeeded", "payment_intent.succeeded"):
            _apply_successful_payment(evt, payload)
        else:
            evt.db_set("processing_status", "Ignored")
            return
        evt.db_set("processing_status", "Processed")
        evt.db_set("processed_at", now_datetime())
    except Exception:
        evt.db_set("processing_status", "Failed")
        evt.db_set("error", frappe.get_traceback()[:1000])
        frappe.log_error(frappe.get_traceback(),
                         f"Webhook processing failed: {event_name}")
        raise


def _apply_successful_payment(evt, payload):
    """For when a real gateway is eventually wired in. Submits the
    existing Draft Sales Invoice (created at intent time) instead of
    creating a new one, then records the transaction as before."""
    from rental_marketplace.services import booking_service

    intent = None
    if payload.get("provider_intent_id"):
        intent_name = frappe.db.get_value(
            "Payment Intent",
            {"provider_intent_id": payload["provider_intent_id"]})
        if intent_name:
            intent = frappe.get_doc("Payment Intent", intent_name)
    booking_name = payload.get("booking") or (intent and intent.booking)
    if not booking_name:
        raise BookingNotFound("Webhook has no resolvable booking.")

    txn_id = payload.get("transaction_id") or f"txn_{evt.provider_event_id}"
    if frappe.db.exists("Payment Transaction",
                        {"provider_transaction_id": txn_id}):
        return  # dedupe at transaction level as well

    booking = frappe.get_doc("Booking", booking_name)
    amount = float(payload.get("amount") or (intent and intent.amount)
                   or booking.amount_due)

    txn = frappe.new_doc("Payment Transaction")
    txn.payment_intent = intent.name if intent else None
    txn.booking = booking.name
    txn.transaction_type = "Charge"
    txn.status = "Succeeded"
    txn.amount = amount
    txn.currency = booking.currency
    txn.provider_transaction_id = txn_id
    txn.processed_at = now_datetime()
    txn.raw_reference = evt.name
    txn.insert(ignore_permissions=True)

    if intent:
        intent.db_set("status", "Succeeded")

    booking.db_set("amount_paid",
                   round((booking.amount_paid or 0) + amount, 2))
    if booking.collect_full_amount:
        platform_total = round(booking.gross_amount or 0, 2)
    else:
        platform_total = round((booking.commission_amount or 0)
                                + (booking.tax_amount or 0), 2)
    booking.db_set("amount_due",
                   round(platform_total - (booking.amount_paid or 0), 2))
    booking.reload()
    if booking.amount_paid >= platform_total:
        booking.db_set("payment_status", "Paid")
        if booking.booking_status == "Pending Payment":
            booking_service.transition(booking, "confirm", system=True,
                                       metadata={"txn": txn.name})
    else:
        booking.db_set("payment_status", "Partially Paid")

    # Sales Invoice is intentionally left in Draft — always, even once
    # the booking is fully paid. It is never submitted anywhere.

    audit.log("payment_received", "Payment Transaction", txn.name,
              metadata={"booking": booking.name, "amount": amount})
    notification_service.send("payment_received", booking.customer,
                              {"booking": booking.name})


def _company():
    """Company used for invoices/payments: Kreatao if it exists, else the site default."""
    if frappe.db.exists("Company", "Kreatao"):
        return "Kreatao"
    return (frappe.defaults.get_global_default("company")
            or frappe.db.get_value("Company", {}, "name"))

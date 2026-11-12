# Rental Marketplace

A production-oriented **tender/bid car-rental marketplace** on Frappe
Framework v16. A customer submits a rental requirement; eligible,
verified suppliers respond with competing offers; the customer accepts
one; the system creates a concurrency-safe booking, takes payment via
an idempotent webhook, and settles suppliers from historical snapshots.

Authoritative chain:
`Rental Request → Tender → Offers → Accepted Offer → Booking → Payment
→ Rental → Refund/Settlement → Completion` — every transition is
validated, authorized, transaction-safe, idempotent, audited.

## Install

```bash
cd frappe-bench/apps && unzip rental_marketplace_app.zip
cd frappe-bench
./env/bin/pip install -e apps/rental_marketplace
bench --site yoursite.local install-app rental_marketplace
bench --site yoursite.local migrate
bench build && bench restart
```

## Post-install checklist

1. **Marketplace Settings** — currency, commission %, tender response
   window, offer validity, payment-hold minutes, webhook secret,
   ranking weights.
2. Create **Marketplace Locations** and confirm the seeded
   **Vehicle Classes**.
3. Create **Marketplace Suppliers** → set status **Verified**, add
   **Service Areas** and **Portal Users** (link to Frappe Users with
   Supplier roles).
4. Add **Marketplace Vehicles**, **Pricing Rules** (at minimum one
   global per vehicle class) and **Cancellation Policies**.
5. Give customers the **Marketplace Customer** role (the spec's
   "Customer" role name collides with ERPNext, hence the prefix).

## Architecture

**Service layer** (`rental_marketplace/services/`) — DocType
controllers validate lifecycle invariants; complex operations live in
services: `tender_service`, `offer_service`, `matching_service`,
`booking_service`, `pricing_service`, `payment_service`,
`cancellation_service`, `settlement_service`, `notification_service`,
plus `audit` and `idempotency`.

**Immutable commercial snapshots** — Tenders snapshot the request;
submitted Offers freeze their financial fields (controller-enforced;
changes only via `offer_service.create_version`, with every version
kept in **Offer Version**); Bookings carry customer/supplier/vehicle/
pricing/policy JSON snapshots. Historical settlements are built from
those snapshots, never from today's rules.

**Server-authoritative pricing** — `pricing_service.compute()` is a
pure Decimal engine with deterministic rule precedence
(supplier+class+location+date > supplier+class+location >
supplier+class > global class; ties by priority then name). Applied
rule IDs are stored in each pricing snapshot. The frontend never
supplies a price.

**Concurrency-safe acceptance** — `booking_service.accept_offer` runs
`SELECT … FOR UPDATE` on the tender, the offer, the vehicle's
availability blocks and conflicting bookings before creating the
booking + reservation block, accepting the offer, rejecting
competitors and awarding the tender in one transaction. Availability
is always derived from **Vehicle Availability Block** records — never
`vehicle.status`.

**Booking state machine** — `booking_service.transition(booking,
action)` is the only way status changes (the Booking controller
rejects direct writes). Each action declares allowed source states,
role gate, side effects, notifications and an audit event.

**Payments** — `POST /api/method/rental_marketplace.api.payment_webhook`
verifies an HMAC-SHA256 signature (`X-Marketplace-Signature` over the
raw body using the settings secret), stores the raw event, dedupes on
`provider_event_id`, enqueues processing, then updates the Payment
Transaction, the booking, the marketplace ledger and notifications.
Deliveries are safe to repeat.

**Marketplace ledger** — Customer Receipt / Marketplace Commission /
Supplier Payable entries on confirmation; Customer Refund /
Commission Reversal / Supplier Adjustment on cancellation. Weekly
settlements consume unsettled ledger rows idempotently.

**Idempotency** — `accept_offer`, `submit_offer`, `create_payment`,
`cancel_booking` and the webhook all accept idempotency keys; reuse
with a different payload returns `IDEMPOTENCY_CONFLICT` (409).

**Errors** — every API returns
`{"success": false, "error": {"code", "message"}}` with stable codes
(`OFFER_EXPIRED`, `TENDER_CLOSED`, `VEHICLE_UNAVAILABLE`, …). Stack
traces never reach customers.

## API surface

| Endpoint (`/api/method/rental_marketplace.api.…`) | Actor |
|---|---|
| `create_rental_request` | Customer |
| `get_rental_request` | Customer |
| `get_tender_offers` (normalized, ranked cards; `sort=` recommended/lowest_price/best_rated/flexible_cancellation) | Customer |
| `accept_offer` (idempotent, locked) | Customer |
| `get_booking` (commission fields stripped for customers) | Customer/Supplier |
| `create_payment` | Customer |
| `cancel_booking` (policy-driven refund) | Customer/Operator |
| `submit_offer` (server-priced) | Supplier |
| `payment_webhook` (guest + signature) | Provider |
| `supplier_transition` (state-machine actions) | Supplier |
| `partner_summary` | Partner |
| `verify_supplier` / `suspend_supplier` | Operator |
| `approve_settlement` | Finance |

## Portals

**Customer** — `/portal` dashboard (requests with live offer counts,
bookings, action-needed banner) → `/request` (form; honours `?ref=`
partner codes) → `/offers/<tender>` (ranked cards + accept) →
`/account/bookings` (pay/cancel).

**Supplier** — `/supplier` dashboard (open tenders, live offers,
upcoming/active/completed rentals, cancellation rate, finance
snapshot) · `/supplier/tenders` (invitation inbox + inline offer
submission) · `/supplier/fleet` (vehicles + active availability
blocks) · `/supplier/bookings` (state-machine actions: confirm →
ready → pickup → activate → return → complete via
`api.supplier_transition`) · `/supplier/finance` (settlements +
unsettled ledger entries).

**Partner** — `/partner` for affiliates/agencies/API partners
(**Marketplace Partner** DocType: unique referral code, share of
marketplace commission). Referral links (`/request?ref=CODE`)
attribute the request → the booking copies the partner and computes
`partner_commission`; a Partner Commission ledger entry posts on
confirmation. The portal shows the referral link, attributed
requests/bookings, earned (completed), pipeline, and paid-out totals.

**Operations** — `/operations` (operator roles only): live counters
(open tenders, pending payments, disputes, failed webhooks), cached
metrics, supplier verification queue with Verify/Reject actions,
settlement approval queue (Finance-gated Approve), recent refunds and
the audit stream. Deep management remains in the Frappe Desk.

## Scheduler

Hourly: `expire_offers`, `close_expired_tenders` (idempotent),
`release_expired_reservations` (payment-hold release). Daily:
pickup/return reminders, stale-supplier detection, payment
reconciliation (flags mismatches to operations — never
auto-corrects), metrics refresh. Weekly: settlement generation.
Monthly: idempotency-key retention cleanup (audit events and webhook
payloads are retained).

## Tests

`rental_marketplace/tests/` — pure unit tests (17) for pricing rule
precedence and amounts, booking state-machine matrix, and refund
calculation; runnable without a site. Integration/permission tests
against a bench site can build on the service layer entrypoints.

## Notes & deviations

- Roles use spec names with two adjustments: **Marketplace Customer**
  (ERPNext "Customer" collision) and supplier scoping via the
  **Supplier Portal User** child table.
- The payment provider is a stub (`provider_intent_id` generated
  locally); wire a real gateway inside `payment_service` — the
  webhook contract (`event_id`, `type`, `provider_intent_id`,
  `amount`, `transaction_id`) stays stable.
- Custom permission types (approve/refund/release) are enforced as
  role gates inside services and controllers (e.g. only Marketplace
  Finance can Approve/Pay settlements).

---

## Installation

Requires Frappe and ERPNext (same major version) on the bench.

```bash
bench get-app https://github.com/thinknxg/car_rental_marketplace.git
bench --site <site> install-app rental_marketplace
bench --site <site> migrate
bench build --app rental_marketplace
bench restart
```

Installing creates the marketplace roles and the 8 vehicle classes. `migrate` also creates the three
custom fields on Booking (`collect_full_amount`, `sales_invoice`, `extras_snapshot`).

## Configuration checklist (nothing below is stored in git)

**Site config** (server only, never commit): `paypal_client_id`, `paypal_client_secret`,
`paypal_sandbox` (1 = sandbox, 0 = live, as a number), `host_name` (https URL of the site).
Do not set `allow_demo_payments` in production.

**Desk > Marketplace Settings**: default currency, default commission %, default tax %, webhook secret.

**Desk > Pricing Rule / Cancellation Policy / Marketplace Location**: your business rules and locations.

**Users**: give staff the roles Marketplace Admin / Operations / Finance; suppliers get Supplier Admin /
Manager / Agent through the Operations page ("Set Login").

**Scheduler** must be enabled: offers expire, tenders close, settlements are generated and payments
reconciled by scheduled jobs.

## Pages

| Route | Purpose |
|---|---|
| /homepage, /fleet, /request | Customer search and rental request |
| /offers/<tender>, /account/bookings, /portal | Compare offers, pay, manage bookings |
| /become-supplier | Supplier application |
| /supplier, /supplier/tenders, /supplier/fleet, /supplier/bookings, /supplier/finance | Supplier portal |
| /partner | Partner commissions |
| /operations | Operations console (month filter, finance, approvals, health) |

## Money flow

- Default: the customer pays commission + tax online; the supplier is paid by the customer at pickup.
- Full payment: the customer pays the full amount online; the supplier is paid by settlement after the
  booking is Completed.
- Booking lifecycle: Pending Payment > Confirmed > Supplier Confirmed > Ready for Pickup > Picked Up >
  Active Rental > Returned > Completed (suppliers advance it from /supplier/bookings).

## Tests

```bash
bench --site <site> run-tests --app rental_marketplace
```

## Known limitations

- PayPal refunds are not sent automatically; refund in PayPal, then mark the Marketplace Refund Processed.
- PayPal orders are captured when the customer returns to the site; there is no webhook recovery for
  abandoned approvals.
- Amounts are rounded to 2 decimals (OMR has 3).
- Sales Invoices are left in Draft.

# Car Rental Marketplace (`rental_marketplace`)

A production-oriented **tender/bid car-rental marketplace** on Frappe
Framework v16. A customer submits a rental requirement; eligible,
verified suppliers respond with competing offers; the customer accepts
one; the system creates a concurrency-safe booking, takes payment via
an idempotent webhook, and settles suppliers from historical snapshots.

Authoritative chain:
`Car Rental Request → Tender → Offers → Accepted Offer → Booking → Payment
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

1. **Car Marketplace Settings** — currency, commission %, tender response
   window, offer validity, payment-hold minutes, webhook secret,
   ranking weights.
2. Create **Marketplace Locations** and confirm the seeded
   **Vehicle Classes**.
3. Create **Marketplace Suppliers** → set status **Verified**, add
   **Service Areas** and **Portal Users** (link to Frappe Users with
   Supplier roles).
4. Add **Marketplace Vehicles**, **Car Pricing Rules** (at minimum one
   global per vehicle class) and **Cancellation Policies**.
5. Give customers the **Car Marketplace Customer** role (the spec's
   "Customer" role name collides with ERPNext, hence the prefix).

## Sharing a bench or site with other apps (v1.1)

This app can be installed on the same bench, and the same site, as
[thinkNXG Rental Marketplace](https://github.com/thinknxg/thinkNXG-Rental-Marketplace)
(`thinknxg_rental_marketplace`) and ERPNext v16. To make that possible v1.1 renamed:

| Before v1.1 | From v1.1 |
|---|---|
| Module `Rental Marketplace` | `Car Rental Marketplace` (folder `car_rental_marketplace/`) |
| DocType `Rental Request` | `Car Rental Request` |
| DocType `Marketplace Settings` | `Car Marketplace Settings` |
| DocType `Marketplace Review` | `Car Marketplace Review` |
| DocType `Marketplace Audit Event` | `Car Marketplace Audit Event` |
| DocType `Pricing Rule` (clashed with ERPNext) | `Car Pricing Rule` |
| Roles `Marketplace Admin / Operations / Finance / Support / Customer` | `Car Marketplace Admin / Operations / Finance / Support / Customer` |
| Pages at `/homepage`, `/portal`, `/request`, `/supplier…`, `/operations`, ... | Under `/car-marketplace/…` (files in `www/car_marketplace/`) |
| Realtime events `marketplace:<event>` | `car_marketplace:<event>` |

The Python package (`rental_marketplace`), API paths (`rental_marketplace.api.*`), all other
DocTypes, naming series and the `Supplier *` roles are unchanged.

**Upgrading an existing site:** `bench --site <site> migrate`. The patches in
`patches/v1_1/` rename the module, DocTypes and roles in place (tables are renamed, rows are
kept), move this app's rules out of ERPNext's `Pricing Rule` table into `Car Pricing Rule`
and give ERPNext its own Pricing Rule DocType back. Take a backup first, as for any migrate.
After upgrading:

- Update any bookmarks, links, QR codes or ads that point at the old root URLs. Only
  `/paypal_return` redirects automatically (for PayPal sessions opened before the upgrade).
  On a site that runs only this app you may add 301s for the old URLs in
  **Website Settings → Route Redirects**.

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

**Customer** — `/car-marketplace/portal` dashboard (requests with live offer counts,
bookings, action-needed banner) → `/car-marketplace/request` (form; honours `?ref=`
partner codes) → `/car-marketplace/offers/<tender>` (ranked cards + accept) →
`/car-marketplace/account/bookings` (pay/cancel).

**Supplier** — `/car-marketplace/supplier` dashboard (open tenders, live offers,
upcoming/active/completed rentals, cancellation rate, finance
snapshot) · `/car-marketplace/supplier/tenders` (invitation inbox + inline offer
submission) · `/car-marketplace/supplier/fleet` (vehicles + active availability
blocks) · `/car-marketplace/supplier/bookings` (state-machine actions: confirm →
ready → pickup → activate → return → complete via
`api.supplier_transition`) · `/car-marketplace/supplier/finance` (settlements +
unsettled ledger entries).

**Partner** — `/car-marketplace/partner` for affiliates/agencies/API partners
(**Marketplace Partner** DocType: unique referral code, share of
marketplace commission). Referral links (`/car-marketplace/request?ref=CODE`)
attribute the request → the booking copies the partner and computes
`partner_commission`; a Partner Commission ledger entry posts on
confirmation. The portal shows the referral link, attributed
requests/bookings, earned (completed), pipeline, and paid-out totals.

**Operations** — `/car-marketplace/operations` (operator roles only): live counters
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

- Roles use spec names with two adjustments: **Car Marketplace Customer**
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

**Desk > Car Marketplace Settings**: default currency, default commission %, default tax %, webhook secret.

**Desk > Car Pricing Rule / Cancellation Policy / Marketplace Location**: your business rules and locations.

**Users**: give staff the roles Car Marketplace Admin / Operations / Finance; suppliers get Supplier Admin /
Manager / Agent through the Operations page ("Set Login").

**Scheduler** must be enabled: offers expire, tenders close, settlements are generated and payments
reconciled by scheduled jobs.

## Pages

| Route | Purpose |
|---|---|
| /car-marketplace, /car-marketplace/fleet, /car-marketplace/request | Customer search and rental request |
| /car-marketplace/offers/<tender>, /car-marketplace/account/bookings, /car-marketplace/portal | Compare offers, pay, manage bookings |
| /car-marketplace/become-supplier | Supplier application |
| /car-marketplace/supplier, /car-marketplace/supplier/tenders, /car-marketplace/supplier/fleet, /car-marketplace/supplier/bookings, /car-marketplace/supplier/finance | Supplier portal |
| /car-marketplace/partner | Partner commissions |
| /car-marketplace/operations | Operations console (month filter, finance, approvals, health) |

## Money flow

- Default: the customer pays commission + tax online; the supplier is paid by the customer at pickup.
- Full payment: the customer pays the full amount online; the supplier is paid by settlement after the
  booking is Completed.
- Booking lifecycle: Pending Payment > Confirmed > Supplier Confirmed > Ready for Pickup > Picked Up >
  Active Rental > Returned > Completed (suppliers advance it from /car-marketplace/supplier/bookings).

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

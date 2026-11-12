"""Server-authoritative pricing engine.

The frontend is NEVER authoritative for price. Every monetary figure
is computed with Decimal, and the applied rule IDs are stored in the
returned snapshot for auditability.

Rule precedence (deterministic):
    supplier + class + location + date-window   (score 15)
  > supplier + class + location                 (score 14)
  > supplier + class                            (score 12)
  > global class                                (score 4)
Score = supplier(8) + vehicle_class(4) + location(2) + date-window(1);
ties break on rule.priority desc, then rule name for stability.
"""

from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe.utils import getdate

from rental_marketplace.services import tax_service

TWO = Decimal("0.01")
EXTRAS_COMMISSION_PCT = Decimal("15")  # flat rate collected now on extras, vs commission_pct for rent


def q2(x):
    return Decimal(str(x or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


# ── Pure functions (unit-testable without a site) ──────────────────────────────

def score_rule(rule, ctx):
    """Return match score or None if the rule does not apply."""
    if rule.get("supplier") and rule["supplier"] != ctx.get("supplier"):
        return None
    if rule.get("vehicle_class") and rule["vehicle_class"] != ctx.get("vehicle_class"):
        return None
    if rule.get("location") and rule["location"] != ctx.get("pickup_location"):
        return None
    if rule.get("country") and rule["country"] != ctx.get("country"):
        return None
    days = ctx.get("rental_days") or 1
    if days < (rule.get("minimum_days") or 1):
        return None
    if days > (rule.get("maximum_days") or 10**6):
        return None
    pickup = ctx.get("pickup_date")
    if rule.get("start_date") and pickup and getdate(pickup) < getdate(rule["start_date"]):
        return None
    if rule.get("end_date") and pickup and getdate(pickup) > getdate(rule["end_date"]):
        return None
    score = 0
    if rule.get("supplier"):
        score += 8
    if rule.get("vehicle_class"):
        score += 4
    if rule.get("location"):
        score += 2
    if rule.get("start_date") or rule.get("end_date"):
        score += 1
    return score


def select_rule(rules, ctx):
    """Deterministic best rule: highest score, then priority, then name."""
    scored = []
    for r in rules:
        s = score_rule(r, ctx)
        if s is not None:
            try:
                priority = int(r.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            scored.append((s, priority, r.get("name") or "", r))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return scored[0][3]


def base_amount_for(rule, rental_days):
    """Weekly/monthly rates apply as cheaper period bundles when defined."""
    days = int(rental_days or 1)
    daily = q2(rule.get("daily_rate"))
    weekly = q2(rule.get("weekly_rate"))
    monthly = q2(rule.get("monthly_rate"))
    remaining = days
    total = Decimal("0")
    if monthly > 0:
        months, remaining = divmod(remaining, 30)
        total += monthly * months
    if weekly > 0:
        weeks, remaining = divmod(remaining, 7)
        total += weekly * weeks
    total += daily * remaining
    return q2(total)


def compute_extras(extra_rows, rental_days, base_amount):
    """extra_rows: [{"pricing_method","price","quantity"}]"""
    total = Decimal("0")
    lines = []
    for row in extra_rows or []:
        qty = int(row.get("quantity") or 1)
        price = q2(row.get("price"))
        method = row.get("pricing_method") or "Per Rental"
        if method == "Per Day":
            amount = price * qty * int(rental_days or 1)
        elif method == "Per Unit":
            amount = price * qty
        elif method == "Percentage":
            amount = q2(Decimal(str(base_amount)) * price / Decimal("100"))
        else:  # Per Rental
            amount = price * qty
        amount = q2(amount)
        total += amount
        lines.append({**row, "amount": float(amount)})
    return q2(total), lines


def compute(ctx, rules, settings):
    """Pure pricing computation. ctx keys:
    supplier, vehicle_class, pickup_location, country, pickup_date,
    rental_days, one_way (bool), delivery (bool), extras (rows),
    discount (absolute), deposit_amount.
    """
    rule = select_rule(rules, ctx)
    if not rule:
        frappe.throw("No pricing rule matches this context.") if frappe else None

    days = int(ctx.get("rental_days") or 1)
    base = base_amount_for(rule, days)
    discount = q2(ctx.get("discount"))
    extras_total, extra_lines = compute_extras(ctx.get("extras"), days, base)
    one_way = q2(rule.get("one_way_fee")) if ctx.get("one_way") else Decimal("0")
    delivery = q2(rule.get("delivery_fee")) if ctx.get("delivery") else Decimal("0")

    taxable = base - discount + extras_total + one_way + delivery
    tax = tax_service.calculate(float(taxable), {
        "tax_percentage": rule.get("tax_percentage"),
        "default_tax_percentage": settings.get("default_tax_percentage")})
    tax_amount = q2(tax["total_tax"])
    gross = q2(taxable + tax_amount)

    commission_pct = rule.get("commission_percentage")
    if commission_pct in (None, "", 0, 0.0):
        commission_pct = ctx.get("supplier_commission_percentage")
    if commission_pct in (None, "", 0, 0.0):
        commission_pct = settings.get("default_commission_percentage") or 0
    rent_taxable = base - discount + one_way + delivery
    rent_commission = q2(rent_taxable * Decimal(str(commission_pct)) / Decimal("100"))
    extras_commission = q2(extras_total * EXTRAS_COMMISSION_PCT / Decimal("100"))
    commission = rent_commission + extras_commission
    commission += q2(rule.get("fixed_commission"))
    supplier_amount = q2(gross - tax_amount - commission)

    return {
        "base_amount": float(base),
        "discount_amount": float(discount),
        "extra_amount": float(extras_total),
        "extra_lines": extra_lines,
        "delivery_amount": float(delivery),
        "one_way_amount": float(one_way),
        "tax_amount": float(tax_amount),
        "gross_amount": float(gross),
        "commission_amount": float(commission),
        "supplier_amount": float(supplier_amount),
        "deposit_amount": float(q2(ctx.get("deposit_amount"))),
        "currency": ctx.get("currency") or settings.get("default_currency") or "OMR",
        "applied_rule_ids": [rule.get("name")],
        "commission_percentage_applied": float(commission_pct or 0),
        "tax_snapshot": tax,
    }


# ── Frappe-bound entrypoint ────────────────────────────────────────────────────

def price_offer(supplier, vehicle_class, pickup_location, pickup_date,
                rental_days, extras=None, discount=0, one_way=False,
                delivery=False, deposit_amount=0, country=None, currency=None,
                vehicle=None):
    settings = frappe.get_single("Marketplace Settings").as_dict()
    sup_comm = frappe.db.get_value("Marketplace Supplier", supplier,
                                   "commission_percentage")
    rules = frappe.get_all(
        "Pricing Rule", filters={"active": 1},
        fields=["name", "supplier", "vehicle_class", "location", "country",
                "start_date", "end_date", "minimum_days", "maximum_days",
                "daily_rate", "weekly_rate", "monthly_rate", "one_way_fee",
                "delivery_fee", "commission_percentage", "fixed_commission",
                "tax_percentage", "priority"])
    ctx = {
        "supplier": supplier, "vehicle_class": vehicle_class,
        "pickup_location": pickup_location, "country": country,
        "pickup_date": pickup_date, "rental_days": rental_days,
        "extras": extras or [], "discount": discount,
        "one_way": one_way, "delivery": delivery,
        "deposit_amount": deposit_amount, "currency": currency,
        "supplier_commission_percentage": sup_comm,
    }
    # If no admin Pricing Rule matches, fall back to the vehicle's own
    # daily/weekly/monthly rate (set by the supplier on their fleet page).
    if vehicle and not select_rule(rules, ctx):
        vrate = frappe.db.get_value(
            "Marketplace Vehicle", vehicle,
            ["daily_rate", "weekly_rate", "monthly_rate"], as_dict=True)
        if vrate and (vrate.daily_rate or 0) > 0:
            rules = rules + [{
                "name": "Vehicle Rate ({})".format(vehicle),
                "daily_rate": vrate.daily_rate,
                "weekly_rate": vrate.weekly_rate or 0,
                "monthly_rate": vrate.monthly_rate or 0,
            }]
    return compute(ctx, rules, settings)

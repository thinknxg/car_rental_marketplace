import json

import frappe
from frappe.utils import now_datetime, add_to_date, getdate, add_days
from rental_marketplace.permissions import is_operator


def get_context(context):
    context.full_width = True
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/operations"
        raise frappe.Redirect
    if not is_operator(frappe.session.user):
        frappe.throw("Operators only.", frappe.PermissionError)
    context.no_cache = 1
    context.is_finance = bool(
        {"Marketplace Finance", "Marketplace Admin", "System Manager"}
        & set(frappe.get_roles()))

    context.metrics = frappe.cache().get_value(
        "rental_marketplace:metrics") or {}
    context.live = {
        "open_tenders": frappe.db.count("Tender", {"status": "Open"}),
        "live_offers": frappe.db.count(
            "Offer", {"status": ["in", ["Submitted", "Shortlisted"]]}),
        "pending_payment": frappe.db.count(
            "Booking", {"booking_status": "Pending Payment"}),
        "active_rentals": frappe.db.count(
            "Booking", {"booking_status": ["in", ["Picked Up",
                                                  "Active Rental"]]}),
        "disputed": frappe.db.count("Booking",
                                    {"booking_status": "Disputed"}),
        "failed_webhooks": frappe.db.count(
            "Payment Webhook Event", {"processing_status": "Failed"}),
    }
    context.pending_suppliers = frappe.get_all(
        "Marketplace Supplier", filters={"status": "Pending Verification"},
        fields=["name", "supplier_name", "legal_name", "business_type",
                "declared_fleet_size", "business_registration_number",
                "tax_registration_number", "business_address",
                "contact_person_name", "contact_email", "contact_phone",
                "country", "application_notes", "trade_license",
                "id_document", "insurance_certificate", "creation"],
        order_by="creation", limit_page_length=20)
    for s in context.pending_suppliers:
        s["locations"] = frappe.get_all(
            "Supplier Service Area", filters={"parent": s.name},
            fields=["location", "supports_delivery", "supports_one_way"])
        for loc in s["locations"]:
            loc["location_name"] = frappe.db.get_value(
                "Marketplace Location", loc.location, "location_name") or loc.location

    context.verified_suppliers = frappe.get_all(
        "Marketplace Supplier", filters={"status": "Verified"},
        fields=["name", "supplier_name", "legal_name", "business_type",
                "declared_fleet_size", "business_registration_number",
                "tax_registration_number", "business_address",
                "contact_person_name", "contact_email", "contact_phone",
                "country", "application_notes", "trade_license",
                "id_document", "insurance_certificate", "creation"],
        order_by="supplier_name", limit_page_length=100)
    for s in context.verified_suppliers:
        s["locations"] = frappe.get_all(
            "Supplier Service Area", filters={"parent": s.name},
            fields=["location", "supports_delivery", "supports_one_way"])
        for loc in s["locations"]:
            loc["location_name"] = frappe.db.get_value(
                "Marketplace Location", loc.location, "location_name") or loc.location
        s["has_login"] = bool(frappe.get_all(
            "Supplier Portal User", filters={"parent": s.name}, limit_page_length=1))

    context.suspended_suppliers = frappe.get_all(
        "Marketplace Supplier", filters={"status": "Suspended"},
        fields=["name", "supplier_name", "contact_email", "country", "creation"],
        order_by="modified desc", limit_page_length=100)
    context.pending_bookings = frappe.get_all(
        "Booking", filters={"booking_status": "Pending Approval"},
        fields=["name", "booking_reference", "customer", "supplier",
                "vehicle", "gross_amount", "currency", "pickup_datetime",
                "creation"], order_by="creation", limit_page_length=20)
    context.pending_settlements = frappe.get_all(
        "Supplier Settlement", filters={"status": "Calculated"},
        fields=["name", "supplier", "period_start", "period_end",
                "net_payable", "currency"],
        order_by="period_start", limit_page_length=20)
    context.recent_refunds = frappe.get_all(
        "Marketplace Refund",
        fields=["name", "booking", "refund_amount", "penalty_amount",
                "currency", "status", "creation"],
        order_by="creation desc", limit_page_length=10)
    context.recent_audit = frappe.get_all(
        "Marketplace Audit Event",
        fields=["created_at", "event_type", "actor", "entity_type",
                "entity_name", "new_state"],
        order_by="created_at desc", limit_page_length=15)

    open_tenders = frappe.get_all(
        "Tender", filters={"status": "Open"},
        fields=["name", "customer", "status", "issue_datetime",
                "response_deadline"],
        order_by="issue_datetime desc", limit_page_length=20)
    for t in open_tenders:
        t["suppliers"] = frappe.get_all(
            "Tender Supplier", filters={"tender": t.name},
            fields=["supplier", "invitation_status", "invited_at",
                    "viewed_at", "responded_at", "declined_at"],
            order_by="invited_at")
    context.active_tenders = open_tenders

    stale_cutoff = add_to_date(now_datetime(), minutes=-30)
    stale_rows = frappe.get_all(
        "Tender Supplier",
        filters={"invitation_status": ["in", ["Invited", "Viewed"]],
                 "invited_at": ["<", stale_cutoff]},
        fields=["name", "tender", "supplier", "invitation_status",
                "invited_at"], order_by="invited_at")
    supplier_names = list({r.supplier for r in stale_rows})
    supplier_contacts = {}
    if supplier_names:
        for s in frappe.get_all(
                "Marketplace Supplier", filters={"name": ["in", supplier_names]},
                fields=["name", "supplier_name", "contact_phone", "contact_email"]):
            supplier_contacts[s.name] = s
    for r in stale_rows:
        c = supplier_contacts.get(r.supplier, {})
        r["supplier_name"] = c.get("supplier_name") or r.supplier
        r["contact_phone"] = c.get("contact_phone")
        r["contact_email"] = c.get("contact_email")
    context.stale_invitations = stale_rows

    # --- Month filter + booking filters (GET params) ---
    import re as _re
    from datetime import date as _date
    from frappe.utils import add_months, get_last_day
    args = frappe.local.form_dict
    raw_month = (args.get("month") or "").strip()
    today = getdate()
    if raw_month == "all":
        m_start = m_end = None
        month_value, period_label = "all", "All time"
    else:
        mm = _re.match(r"^(\d{4})-(\d{2})$", raw_month)
        if mm and 1 <= int(mm.group(2)) <= 12:
            y, mo = int(mm.group(1)), int(mm.group(2))
        else:
            y, mo = today.year, today.month
        m_start = _date(y, mo, 1)
        m_end = get_last_day(m_start)
        month_value, period_label = m_start.strftime("%Y-%m"), m_start.strftime("%B %Y")
    context.period = {"start": m_start, "end": m_end}
    context.month_value, context.period_label = month_value, period_label
    context.month_options = [{"value": "all", "label": "All time"}] + [
        {"value": add_months(today.replace(day=1), -i).strftime("%Y-%m"),
         "label": add_months(today.replace(day=1), -i).strftime("%B %Y")} for i in range(24)]

    f_status = args.get("status") or ""
    f_supplier = args.get("supplier") or ""
    context.filters = {"status": f_status, "supplier": f_supplier}
    context.all_suppliers = frappe.get_all(
        "Marketplace Supplier", fields=["name", "supplier_name"],
        order_by="supplier_name", limit_page_length=200)
    context.booking_statuses = [x for x in frappe.get_meta("Booking").get_field(
        "booking_status").options.split("\n") if x]

    booking_filters = {"booking_status": ["not in", ["Cancelled"]]}
    if f_status:
        booking_filters["booking_status"] = f_status
    if f_supplier:
        booking_filters["supplier"] = f_supplier
    if m_start:
        booking_filters["pickup_datetime"] = ["between", [f"{m_start} 00:00:00", f"{m_end} 23:59:59"]]

    booking_rows = frappe.get_all(
        "Booking", filters=booking_filters,
        fields=["name", "booking_reference", "customer", "supplier",
                "vehicle", "booking_status", "payment_status",
                "pickup_location", "dropoff_location", "pickup_datetime",
                "dropoff_datetime", "currency", "gross_amount",
                "amount_paid", "amount_due"],
        order_by="pickup_datetime desc", limit_page_length=100)
    vehicle_names = list({b.vehicle for b in booking_rows if b.vehicle})
    vehicle_info = {}
    if vehicle_names:
        for v in frappe.get_all(
                "Marketplace Vehicle", filters={"name": ["in", vehicle_names]},
                fields=["name", "display_name", "make", "model", "model_year"]):
            vehicle_info[v.name] = v
    customer_ids = list({b.customer for b in booking_rows if b.customer})
    customer_names = {}
    if customer_ids:
        for c in frappe.get_all(
                "Customer Profile", filters={"user": ["in", customer_ids]},
                fields=["user", "first_name", "last_name"]):
            full = " ".join(filter(None, [c.first_name, c.last_name])).strip()
            if full:
                customer_names[c.user] = full
        # Fall back to the User record's name (set at signup) for anyone
        # who hasn't filled in a Customer Profile yet.
        missing = [c for c in customer_ids if c not in customer_names]
        if missing:
            for u in frappe.get_all(
                    "User", filters={"name": ["in", missing]},
                    fields=["name", "full_name"]):
                if u.full_name:
                    customer_names[u.name] = u.full_name
    for b in booking_rows:
        v = vehicle_info.get(b.vehicle, {})
        b["vehicle_label"] = v.get("display_name") or " ".join(
            filter(None, [v.get("make"), v.get("model"),
                          str(v.get("model_year") or "")])).strip() or b.vehicle
        b["customer_label"] = customer_names.get(b.customer, b.customer)
    context.all_bookings = booking_rows

    context.chart_labels = "[]"
    context.chart_revenue = "[]"

    # --- Analytics: booking status breakdown ---
    status_rows = frappe.db.sql(f"""
        select booking_status, count(*) as cnt
        from `tabBooking`
        where 1=1 {_between("pickup_datetime", m_start, m_end)}{(" and supplier = " + frappe.db.escape(f_supplier)) if f_supplier else ""}
        group by booking_status
        order by cnt desc
    """, as_dict=True)
    context.chart_status_labels = json.dumps([r.booking_status for r in status_rows])
    context.chart_status_counts = json.dumps([r.cnt for r in status_rows])

    _extras(context)
    return context


# ── Dashboard extras (premium console) ─────────────────────────────────────────
# Everything below is read-only and wrapped defensively: a failure in one widget
# must never take the whole operations page down.

THREE_DECIMAL = {"OMR", "KWD", "BHD", "JOD", "TND"}


def _money(value, currency=None):
    cur = currency or frappe.db.get_single_value(
        "Marketplace Settings", "default_currency") or "OMR"
    prec = 3 if cur in THREE_DECIMAL else 2
    return f"{cur} {float(value or 0):,.{prec}f}"


def _daily(sql, since, days=14, params=()):
    """Run a `select d, v ...` query and return a dense list of `days` floats."""
    rows = frappe.db.sql(sql, (since,) + tuple(params), as_dict=True)
    by_day = {str(r.d): float(r.v or 0) for r in rows}
    return [by_day.get(str(add_days(since, i)), 0) for i in range(days)]


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "operations dashboard widget")
        return default


def _delta_pct(now_val, prev_val):
    if not prev_val:
        return None
    return round((now_val - prev_val) * 100.0 / prev_val, 1)


def _between(col, start, end):
    """SQL fragment limiting `col` to a month; empty string means All time."""
    if not start:
        return ""
    return f" and date({col}) between '{start}' and '{end}'"


def _fx(col="currency"):
    """SQL multiplier converting an amount in `col` currency to the site default
    currency, using the same fixed pegs as paypal_gateway (OMR/AED/USD)."""
    from rental_marketplace.services.paypal_gateway import FX_TO_USD
    default = (frappe.db.get_single_value("Marketplace Settings", "default_currency") or "OMR").upper()
    base = FX_TO_USD.get(default) or 1.0
    whens = " ".join(f"when '{c}' then {rate / base:.8f}" for c, rate in FX_TO_USD.items())
    return f"(case upper({col}) {whens} else 1 end)"


def _bucket(table, value, date_col, where, start, end, today):
    """One number per day of the chosen month, or per month (last 12) for All time."""
    from frappe.utils import add_months
    if start:
        n = (end - start).days + 1
        rows = frappe.db.sql(
            f"select date({date_col}) d, sum({value}) v from {table} "
            f"where date({date_col}) between %s and %s{where} group by d", (start, end), as_dict=True)
        by = {str(r.d): float(r.v or 0) for r in rows}
        return [by.get(str(add_days(start, i)), 0) for i in range(n)]
    first = add_months(today.replace(day=1), -11)
    rows = frappe.db.sql(
        f"select date_format({date_col},'%%Y-%%m') m, sum({value}) v from {table} "
        f"where date({date_col})>=%s{where} group by m", (first,), as_dict=True)
    by = {r.m: float(r.v or 0) for r in rows}
    return [by.get(add_months(first, i).strftime("%Y-%m"), 0) for i in range(12)]


def _extras(context):
    from frappe.utils import add_months
    context.money = _money
    today = getdate()
    start, end = context.period["start"], context.period["end"]
    label = context.period_label
    cur = frappe.db.get_single_value("Marketplace Settings", "default_currency") or "OMR"
    context.currency = cur
    nc = "booking_status not in ('Cancelled','No Show')"
    paid = "payment_status = 'Paid'"
    fx = _fx()
    f_status = context.filters.get("status") or ""
    f_supplier = context.filters.get("supplier") or ""
    esc = frappe.db.escape
    if f_status:
        nc = f"booking_status = {esc(f_status)}"
    bkf = f" and supplier = {esc(f_supplier)}" if f_supplier else ""
    bkf_b = f" and b.supplier = {esc(f_supplier)}" if f_supplier else ""
    led_f = bkf
    ref_f = (f" and booking in (select name from `tabBooking` where supplier = {esc(f_supplier)})"
             if f_supplier else "")
    context.filter_supplier_name = (frappe.db.get_value(
        "Marketplace Supplier", f_supplier, "supplier_name") or f_supplier) if f_supplier else ""
    pickup = _between("pickup_datetime", start, end) + bkf
    created = _between("creation", start, end)
    BK = "`tabBooking`"

    def total(sql):
        return float(frappe.db.sql(sql)[0][0] or 0)

    # --- totals line (replaces the cached all-time metrics) ---------------------
    def metrics():
        n = lambda t, col="creation", extra="": int(frappe.db.sql(
            f"select count(*) from `tab{t}` where 1=1{_between(col, start, end)}{extra}")[0][0] or 0)
        return {"requests_created": n("Rental Request"), "tenders_created": n("Tender"),
                "offers_submitted": n("Offer", extra=" and status!='Draft'"),
                "bookings": n("Booking", extra=" and booking_status!='Cancelled'" + bkf),
                "completed": n("Booking", extra=" and booking_status='Completed'" + bkf),
                "cancelled": n("Booking", extra=" and booking_status='Cancelled'" + bkf),
                "refreshed_at": label}
    context.metrics = _safe(metrics, {})
    mt = context.metrics or {}
    context.period_empty = not (mt.get("requests_created") or mt.get("tenders_created")
                                or mt.get("bookings"))

    # --- finance strip: collected money only, for the chosen month ---------------
    def finance():
        refund_fx = _fx()
        comm_s = _bucket(BK, f"commission_amount*{fx}", "pickup_datetime", f" and {nc} and {paid}{bkf}", start, end, today)
        part_s = _bucket(BK, f"partner_commission*{fx}", "pickup_datetime", f" and {nc} and {paid}{bkf}", start, end, today)
        pay_s = _bucket("`tabMarketplace Ledger Entry`", f"amount*{_fx()}", "posting_datetime",
                        f" and entry_type='Supplier Payable' and settled=0{led_f}", start, end, today)
        ref_s = _bucket("`tabMarketplace Refund`", f"refund_amount*{refund_fx}", "creation",
                        f" and status='Processed'{ref_f}", start, end, today)
        gross_paid = total(f"select coalesce(sum(gross_amount*{fx}),0) from {BK} where {nc} and {paid}{pickup}")
        comm = total(f"select coalesce(sum(commission_amount*{fx}),0) from {BK} where {nc} and {paid}{pickup}")
        partner = total(f"select coalesce(sum(partner_commission*{fx}),0) from {BK} where {nc} and {paid}{pickup}")
        refunds = total(f"select coalesce(sum(refund_amount*{refund_fx}),0) from `tabMarketplace Refund` "
                        f"where status='Processed'{created}{ref_f}")
        payables = total(f"select coalesce(sum(amount*{_fx()}),0) from `tabMarketplace Ledger Entry` "
                         f"where entry_type='Supplier Payable' and settled=0{led_f}")
        n_sup = frappe.db.sql("select count(distinct supplier) from `tabMarketplace Ledger Entry` "
                              f"where entry_type='Supplier Payable' and settled=0{led_f}")[0][0] or 0
        return {
            "commission_total": comm, "commission_delta": None,
            "commission_rate": round(comm * 100.0 / gross_paid, 1) if gross_paid else 0,
            "commission_series": comm_s,
            "payables_total": payables, "payable_suppliers": n_sup, "payable_series": pay_s,
            "refunds_total": refunds, "refund_series": ref_s,
            "refund_pct": round(refunds * 100.0 / comm, 1) if comm else 0,
            "partner_total": partner, "partner_series": part_s,
            "partners_active": frappe.db.count("Marketplace Partner", {"status": "Active"}),
        }
    context.fin = _safe(finance, {})

    # --- performance chart for the chosen month ---------------------------------
    def series():
        if start:
            labels = [add_days(start, i).strftime("%d %b") for i in range((end - start).days + 1)]
        else:
            first = add_months(today.replace(day=1), -11)
            labels = [add_months(first, i).strftime("%b %Y") for i in range(12)]
        return json.dumps({
            "labels": labels,
            "revenue": _bucket(BK, f"gross_amount*{fx}", "pickup_datetime", f" and {nc}{bkf}", start, end, today),
            "bookings": _bucket(BK, "1", "pickup_datetime", f" and {nc}{bkf}", start, end, today),
            "commission": _bucket(BK, f"commission_amount*{fx}", "pickup_datetime", f" and {nc} and {paid}{bkf}", start, end, today)})
    context.chart_series = _safe(series, json.dumps({"labels": [], "revenue": [], "bookings": [], "commission": []}))
    context.pending_due = _safe(lambda: total(
        f"select coalesce(sum(amount_due*{fx}),0) from {BK} where booking_status='Pending Payment'"), 0)

    # --- conversion funnel: activity created in the month -----------------------
    def funnel():
        cnt = lambda t, extra="": int(frappe.db.sql(
            f"select count(*) from `tab{t}` where 1=1{created}{extra}")[0][0] or 0)
        requests = cnt("Rental Request")
        offered = int(frappe.db.sql(
            "select count(distinct t.rental_request) from `tabOffer` o join `tabTender` t on t.name=o.tender "
            f"where o.status!='Draft'{_between('o.creation', start, end)}")[0][0] or 0)
        booked = cnt("Booking", " and booking_status!='Cancelled'")
        paid_n = cnt("Booking", " and booking_status!='Cancelled' and payment_status in ('Paid','Partially Paid')")
        done = cnt("Booking", " and booking_status='Completed'")
        stages = [("Requests", requests), ("Offered", offered), ("Booked", booked), ("Paid", paid_n), ("Completed", done)]
        base = requests or max([c for _, c in stages] or [1]) or 1
        return {"stages": [{"label": l, "count": c, "pct": min(100, round(c * 100.0 / base))} for l, c in stages],
                "end_to_end": min(100, round(done * 100.0 / base))}
    context.funnel = _safe(funnel, {"stages": [], "end_to_end": 0})

    # --- top pickup locations for the chosen month -------------------------------
    def locations():
        rows = frappe.db.sql(
            "select coalesce(l.location_name, b.pickup_location) name, count(*) c "
            "from `tabBooking` b left join `tabMarketplace Location` l on l.name=b.pickup_location "
            f"where b.{nc} and b.pickup_location is not null{_between('b.pickup_datetime', start, end)}{bkf_b} "
            "group by b.pickup_location order by c desc limit 5", as_dict=True)
        top = max([r.c for r in rows] or [1])
        return [{"name": r.name, "count": r.c, "pct": round(r.c * 100.0 / top)} for r in rows]
    context.top_locations = _safe(locations, [])

    # --- supplier performance for the chosen month -------------------------------
    def performance():
        bk = {r.supplier: r for r in frappe.db.sql(
            f"select supplier, count(*) c, coalesce(sum(gross_amount*{fx}),0) rev from {BK} "
            f"where {nc}{pickup} group by supplier", as_dict=True)}
        inv = {r.supplier: r for r in frappe.db.sql(
            "select supplier, count(*) total, sum(invitation_status='Responded') responded, "
            "avg(case when responded_at is not null then timestampdiff(MINUTE, invited_at, responded_at) end) avg_min "
            f"from `tabTender Supplier` where 1=1{_between('invited_at', start, end)} group by supplier", as_dict=True)}
        out = []
        for s_ in frappe.get_all("Marketplace Supplier", filters=dict({"status": "Verified"}, **({"name": f_supplier} if f_supplier else {})),
                                 fields=["name", "supplier_name", "declared_fleet_size", "rating"]):
            b, i = bk.get(s_.name), inv.get(s_.name)
            rate = round(float(i.responded or 0) * 100.0 / i.total) if i and i.total else None
            out.append({"name": s_.name, "supplier_name": s_.supplier_name, "fleet": s_.declared_fleet_size,
                        "rating": s_.rating or None, "bookings": b.c if b else 0,
                        "revenue": float(b.rev) if b else 0, "response_rate": rate,
                        "avg_minutes": round(float(i.avg_min)) if i and i.avg_min is not None else None})
        out.sort(key=lambda r: (-r["bookings"], -r["revenue"]))
        return out[:8]
    context.supplier_perf = _safe(performance, [])

    # --- refunds and audit stream for the chosen month ---------------------------
    rng = {"between": [f"{start} 00:00:00", f"{end} 23:59:59"]} if start else None
    rf = {"creation": ["between", rng["between"]]} if rng else {}
    if f_supplier:
        rf["booking"] = ["in", frappe.get_all("Booking", filters={"supplier": f_supplier}, pluck="name",
                                              limit_page_length=5000) or [""]]
    context.recent_refunds = frappe.get_all(
        "Marketplace Refund", filters=rf,
        fields=["name", "booking", "refund_amount", "penalty_amount", "currency", "status", "creation"],
        order_by="creation desc", limit_page_length=10)
    context.recent_audit = frappe.get_all(
        "Marketplace Audit Event", filters={"created_at": ["between", rng["between"]]} if rng else {},
        fields=["created_at", "event_type", "actor", "entity_type", "entity_name", "new_state"],
        order_by="created_at desc", limit_page_length=15)

    # --- system health ------------------------------------------------------------
    def health():
        items = []
        try:
            from frappe.utils.scheduler import is_scheduler_inactive
            off = is_scheduler_inactive(verbose=False)
        except Exception:
            off = False
        items.append({"title": "Scheduler", "detail": "Offer expiry, tender closing, settlements",
                      "state": "bad" if off else "ok", "label": "Disabled" if off else "Running"})
        failed = frappe.db.count("Payment Webhook Event", {
            "processing_status": "Failed", "creation": [">=", add_to_date(now_datetime(), hours=-24)]})
        items.append({"title": "Payment webhooks", "detail": f"{failed} failed in 24 hours",
                      "state": "ok" if not failed else "bad", "label": "Healthy" if not failed else "Failing"})
        job = frappe.db.get_value("Scheduled Job Type",
                                  {"method": "rental_marketplace.tasks.reconcile_payments"}, "name")
        last = frappe.db.get_value("Scheduled Job Log", {"scheduled_job_type": job},
                                   ["status", "creation"], order_by="creation desc", as_dict=True) if job else None
        if last:
            ok = last.status == "Complete"
            items.append({"title": "Reconciliation",
                          "detail": "Last run " + frappe.utils.format_datetime(last.creation, "d MMM, HH:mm"),
                          "state": "ok" if ok else "warn", "label": "Clean" if ok else last.status})
        else:
            items.append({"title": "Reconciliation", "detail": "No run recorded yet", "state": "warn", "label": "Pending"})
        queued = frappe.db.count("Email Queue", {"status": "Not Sent"})
        items.append({"title": "Email queue", "detail": f"{queued} notifications pending",
                      "state": "ok" if queued < 20 else "warn", "label": "Normal" if queued < 20 else "Backlog"})
        return items
    context.health = _safe(health, [])
    context.health_warnings = sum(1 for h in context.health if h["state"] != "ok")
    context.locations_admin = frappe.get_all(
        "Marketplace Location",
        fields=["name", "location_name", "location_type", "city", "country", "active"],
        order_by="location_name", limit_page_length=500)
    context.location_types = ["Airport", "City", "Port", "Hotel Zone", "Other"]

    # --- header ---------------------------------------------------------------------
    user = frappe.session.user
    full = frappe.db.get_value("User", user, "full_name") or user
    roles = set(frappe.get_roles())
    role_label = next((r for r in ("Marketplace Admin", "Marketplace Operations",
                                   "Marketplace Finance", "System Manager") if r in roles), "Operator")
    context.op_user = {"name": full, "role": role_label,
                       "initials": "".join(p[0] for p in full.split()[:2]).upper() or "OP"}
    context.alert_count = (len(context.stale_invitations) + len(context.pending_suppliers)
                           + len(context.pending_bookings) + len(context.pending_settlements)
                           + context.live["disputed"] + context.live["failed_webhooks"])
    context.fin_delta = lambda v: "" if v is None else (f"+{v}%" if v >= 0 else f"{v}%")

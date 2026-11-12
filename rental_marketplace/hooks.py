app_name = "rental_marketplace"
app_title = "Rental Marketplace"
app_publisher = "Kreatao"
app_description = "Tender/bid-based car rental marketplace"
app_email = "dev@kreatao.com"
app_license = "MIT"

required_apps = ["erpnext"]

after_install = "rental_marketplace.install.after_install"
after_migrate = ["rental_marketplace.install.ensure_custom_fields"]

# ── Fixtures ───────────────────────────────────────────────────────────────────
fixtures = [
    {"dt": "Role", "filters": [["role_name", "in", [
        "Marketplace Admin", "Marketplace Operations", "Marketplace Finance",
        "Marketplace Support", "Supplier Admin", "Supplier Manager",
        "Supplier Agent", "Supplier Finance", "Marketplace Customer"]]]},
]

# ── Doc events ─────────────────────────────────────────────────────────────────
doc_events = {
    "Booking": {
        "on_update": "rental_marketplace.services.booking_service.on_booking_update",
    },
}

# ── Scheduler ──────────────────────────────────────────────────────────────────
scheduler_events = {
    "hourly": [
        "rental_marketplace.tasks.expire_offers",
        "rental_marketplace.tasks.close_expired_tenders",
        "rental_marketplace.tasks.release_expired_reservations",
    ],
    "daily": [
        "rental_marketplace.tasks.send_pickup_reminders",
        "rental_marketplace.tasks.send_return_reminders",
        "rental_marketplace.tasks.detect_stale_supplier_responses",
        "rental_marketplace.tasks.reconcile_payments",
        "rental_marketplace.tasks.refresh_marketplace_metrics",
    ],
    "weekly": [
        "rental_marketplace.tasks.generate_settlement_periods",
    ],
    "monthly": [
        "rental_marketplace.tasks.archive_old_events",
    ],
}

# ── Website routes ─────────────────────────────────────────────────────────────
website_route_rules = [
    {"from_route": "/homepage",             "to_route": "homepage"},
    {"from_route": "/portal",               "to_route": "portal_home"},
    {"from_route": "/request",              "to_route": "request"},
    {"from_route": "/fleet",                "to_route": "fleet"},
    {"from_route": "/offers/<tender_id>",   "to_route": "offers"},
    {"from_route": "/account/bookings",     "to_route": "my_bookings"},
    {"from_route": "/supplier",             "to_route": "supplier_home"},
    {"from_route": "/supplier/tenders",     "to_route": "supplier_tenders"},
    {"from_route": "/supplier/fleet",       "to_route": "supplier_fleet"},
    {"from_route": "/supplier/fleet/<vehicle_id>", "to_route": "supplier_vehicle_detail"},
    {"from_route": "/supplier/bookings",    "to_route": "supplier_bookings"},
    {"from_route": "/supplier/bookings/<booking_id>", "to_route": "supplier_booking_detail"},
    {"from_route": "/supplier/finance",     "to_route": "supplier_finance"},
    {"from_route": "/partner",              "to_route": "partner_portal"},
    {"from_route": "/operations",           "to_route": "operations"},
    {"from_route": "/become-supplier",      "to_route": "become_supplier"},
]

portal_menu_items = [
    {"title": "My Marketplace", "route": "/portal",
     "role": "Marketplace Customer"},
    {"title": "Supplier Dashboard", "route": "/supplier",
     "role": "Supplier Agent"},
]

# ── Permissions ────────────────────────────────────────────────────────────────
permission_query_conditions = {
    "Rental Request":      "rental_marketplace.permissions.rental_request_query",
    "Tender Supplier":     "rental_marketplace.permissions.tender_supplier_query",
    "Offer":               "rental_marketplace.permissions.offer_query",
    "Booking":             "rental_marketplace.permissions.booking_query",
    "Marketplace Vehicle": "rental_marketplace.permissions.supplier_scoped_query",
    "Supplier Settlement": "rental_marketplace.permissions.supplier_scoped_query",
    "Payment Intent":      "rental_marketplace.permissions.customer_scoped_query",
    "Marketplace Refund":  "rental_marketplace.permissions.customer_scoped_query",
}

has_permission = {
    "Offer":   "rental_marketplace.permissions.offer_has_permission",
    "Booking": "rental_marketplace.permissions.booking_has_permission",
}

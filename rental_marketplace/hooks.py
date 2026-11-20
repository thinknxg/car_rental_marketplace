app_name = "rental_marketplace"
app_title = "Car Rental Marketplace"
app_publisher = "Kreatao"
app_description = "Tender/bid-based car rental marketplace"
app_email = "dev@kreatao.com"
app_license = "MIT"

required_apps = ["erpnext"]

after_install = "rental_marketplace.install.after_install"
after_migrate = [
    "rental_marketplace.install.ensure_roles",
    "rental_marketplace.install.ensure_custom_fields",
]

# ── Fixtures ───────────────────────────────────────────────────────────────────
fixtures = [
    {"dt": "Role", "filters": [["role_name", "in", [
        "Car Marketplace Admin", "Car Marketplace Operations", "Car Marketplace Finance",
        "Car Marketplace Support", "Supplier Admin", "Supplier Manager",
        "Supplier Agent", "Supplier Finance", "Car Marketplace Customer"]]]},
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
# Every public page lives under /car-marketplace so this app can share a site with
# other apps (Frappe's own /portal, thinknxg_rental_marketplace's /marketplace, ...).
# Page files are in www/car_marketplace/; these rules give them hyphenated URLs.
_P = "/car-marketplace"
website_route_rules = [
    {"from_route": _P,                              "to_route": "car_marketplace/homepage"},
    {"from_route": _P + "/portal",                  "to_route": "car_marketplace/portal_home"},
    {"from_route": _P + "/request",                 "to_route": "car_marketplace/request"},
    {"from_route": _P + "/fleet",                   "to_route": "car_marketplace/fleet"},
    {"from_route": _P + "/car",                     "to_route": "car_marketplace/car"},
    {"from_route": _P + "/checkout",                "to_route": "car_marketplace/checkout"},
    {"from_route": _P + "/paypal-return",           "to_route": "car_marketplace/paypal_return"},
    {"from_route": _P + "/offers/<tender_id>",      "to_route": "car_marketplace/offers"},
    {"from_route": _P + "/account/bookings",        "to_route": "car_marketplace/my_bookings"},
    {"from_route": _P + "/supplier",                "to_route": "car_marketplace/supplier_home"},
    {"from_route": _P + "/supplier/tenders",        "to_route": "car_marketplace/supplier_tenders"},
    {"from_route": _P + "/supplier/fleet",          "to_route": "car_marketplace/supplier_fleet"},
    {"from_route": _P + "/supplier/fleet/<vehicle_id>", "to_route": "car_marketplace/supplier_vehicle_detail"},
    {"from_route": _P + "/supplier/bookings",       "to_route": "car_marketplace/supplier_bookings"},
    {"from_route": _P + "/supplier/bookings/<booking_id>", "to_route": "car_marketplace/supplier_booking_detail"},
    {"from_route": _P + "/supplier/finance",        "to_route": "car_marketplace/supplier_finance"},
    {"from_route": _P + "/partner",                 "to_route": "car_marketplace/partner_portal"},
    {"from_route": _P + "/operations",              "to_route": "car_marketplace/operations"},
    {"from_route": _P + "/become-supplier",         "to_route": "car_marketplace/become_supplier"},
]

# PayPal sessions opened before the route move still return to the old URL.
# The path is specific to this app, so keeping it as a redirect claims nothing generic.
website_redirects = [
    {"source": "/paypal_return", "target": _P + "/paypal-return", "forward_query_parameters": True},
]

portal_menu_items = [
    {"title": "My Marketplace", "route": _P + "/portal",
     "role": "Car Marketplace Customer"},
    {"title": "Supplier Dashboard", "route": _P + "/supplier",
     "role": "Supplier Agent"},
]

# ── Permissions ────────────────────────────────────────────────────────────────
permission_query_conditions = {
    "Car Rental Request":      "rental_marketplace.permissions.rental_request_query",
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

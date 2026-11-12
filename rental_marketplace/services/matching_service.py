"""Supplier matching engine.

matching_service.find_eligible_suppliers(request_doc)
    → [{"supplier": name, "vehicles": [...], "snapshot": {...}}, ...]

Eligibility (spec): supplier active + verified + not blocked; vehicle
active + class compatible + seat count; pickup/dropoff serviceable;
duration supported; driver age satisfied.

Availability is intentionally NOT pre-filtered here — it is enforced
at offer submission and atomically at acceptance; matching only needs
plausible candidates and must stay cheap enough to enqueue.
"""

import frappe
from frappe.utils import get_datetime


def find_eligible_suppliers(request):
    duration_days = max(
        (get_datetime(request.dropoff_datetime)
         - get_datetime(request.pickup_datetime)).days, 1)

    suppliers = frappe.get_all(
        "Marketplace Supplier",
        filters={"active": 1, "status": "Verified"},
        fields=["name", "supplier_name", "rating", "reliability_score"])

    results = []
    for sup in suppliers:
        areas = frappe.get_all(
            "Supplier Service Area",
            filters={"parent": sup.name},
            fields=["location", "supports_one_way"])
        served = {a.location for a in areas}
        if request.pickup_location not in served:
            continue
        if request.dropoff_location and \
                request.dropoff_location != request.pickup_location:
            one_way_ok = any(a.location == request.dropoff_location or
                             a.supports_one_way for a in areas)
            if not one_way_ok:
                continue

        vfilters = {"supplier": sup.name, "active": 1,
                    "vehicle_class": request.vehicle_class}
        if request.transmission:
            vfilters["transmission"] = request.transmission
        if request.fuel_type:
            vfilters["fuel_type"] = request.fuel_type
        if request.seats_required:
            vfilters["seats"] = [">=", request.seats_required]
        vehicles = frappe.get_all("Marketplace Vehicle", filters=vfilters,
                                  fields=["name", "display_name", "seats"])
        if not vehicles:
            continue

        results.append({
            "supplier": sup.name,
            "vehicles": [v.name for v in vehicles],
            "snapshot": {
                "supplier_name": sup.supplier_name,
                "rating": sup.rating,
                "reliability_score": sup.reliability_score,
                "matched_vehicle_count": len(vehicles),
                "duration_days": duration_days,
                "criteria": {
                    "vehicle_class": request.vehicle_class,
                    "pickup_location": request.pickup_location,
                    "seats_required": request.seats_required,
                },
            },
        })
    return results

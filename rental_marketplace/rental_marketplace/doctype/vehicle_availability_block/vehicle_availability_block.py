import frappe
from frappe.model.document import Document


class VehicleAvailabilityBlock(Document):
    """Availability is derived from blocks — never from Vehicle.status.
    Booking-type blocks are created inside the locked acceptance
    transaction (services.booking_service); manual blocks validate
    overlap here for operator hygiene.
    """

    def validate(self):
        if self.end_datetime <= self.start_datetime:
            frappe.throw("Block end must be after start.")
        if self.flags.skip_overlap_check:
            return  # created inside a locked reservation transaction
        conflict = frappe.db.sql(
            """
            SELECT name FROM `tabVehicle Availability Block`
            WHERE vehicle=%(vehicle)s AND name!=%(name)s AND status='Active'
              AND start_datetime < %(end)s AND end_datetime > %(start)s
            LIMIT 1
            """,
            {"vehicle": self.vehicle, "name": self.name or "",
             "start": self.start_datetime, "end": self.end_datetime})
        if conflict:
            frappe.throw(
                f"Vehicle already blocked in this period ({conflict[0][0]}).")

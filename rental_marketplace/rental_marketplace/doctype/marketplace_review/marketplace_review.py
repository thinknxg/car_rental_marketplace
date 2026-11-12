import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class MarketplaceReview(Document):

    def before_insert(self):
        self.created_at = now_datetime()
        booking = frappe.db.get_value(
            "Booking", self.booking,
            ["booking_status", "customer", "supplier", "vehicle"], as_dict=True)
        if not booking:
            frappe.throw("Booking not found.")
        if booking.booking_status != "Completed":
            frappe.throw("Only completed rentals can be reviewed.")
        if booking.customer != frappe.session.user and \
                "Marketplace Admin" not in frappe.get_roles():
            frappe.throw("You can only review your own bookings.")
        self.customer = booking.customer
        self.supplier = booking.supplier
        self.vehicle = booking.vehicle

    def on_update(self):
        # Recompute supplier aggregate rating; suppliers cannot alter reviews.
        avg = frappe.db.sql(
            """SELECT AVG(rating) FROM `tabMarketplace Review`
               WHERE supplier=%s AND status='Published'""", self.supplier)[0][0]
        if avg is not None:
            frappe.db.set_value("Marketplace Supplier", self.supplier,
                                "rating", round(float(avg), 2))

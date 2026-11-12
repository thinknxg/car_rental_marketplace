import frappe
from frappe.model.document import Document


class Booking(Document):
    """Status may only change through
    booking_service.transition(booking, action). Arbitrary
    `booking.booking_status = X; booking.save()` is rejected.
    """

    def validate(self):
        # amount_due tracks only the platform's share (commission + tax) —
        # the rest is paid directly to the vendor and isn't collected here.
        platform_total = (self.commission_amount or 0) + (self.tax_amount or 0)
        self.amount_due = round(platform_total - (self.amount_paid or 0), 2)
        if self.is_new():
            if not self.flags.via_service:
                frappe.throw("Bookings are created by accepting an offer, "
                             "not manually.")
            return
        old = self.get_doc_before_save()
        if old and old.booking_status != self.booking_status \
                and not self.flags.via_transition:
            frappe.throw(
                "Booking status changes must use "
                "booking_service.transition().")

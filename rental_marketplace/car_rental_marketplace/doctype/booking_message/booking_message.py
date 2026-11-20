import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime, add_to_date

RATE_LIMIT_PER_HOUR = 20


class BookingMessage(Document):

    def before_insert(self):
        self.created_at = now_datetime()
        # basic anti-abuse rate limiting before any open-ended messaging
        recent = frappe.db.count("Booking Message", {
            "sender": frappe.session.user,
            "created_at": [">", add_to_date(now_datetime(), hours=-1)]})
        if recent >= RATE_LIMIT_PER_HOUR:
            frappe.throw("Message rate limit reached. Try again later.")

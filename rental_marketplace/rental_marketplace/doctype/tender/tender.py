import frappe
from frappe.model.document import Document

TERMINAL = ("Closed", "Awarded", "Cancelled", "Expired")


class Tender(Document):

    def validate(self):
        if not self.is_new():
            old = self.get_doc_before_save()
            if old and old.status in TERMINAL and self.status != old.status \
                    and not self.flags.via_service:
                frappe.throw(f"Tender is {old.status} — status is final.")

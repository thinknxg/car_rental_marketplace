import frappe
from frappe.model.document import Document


class MarketplaceSupplier(Document):

    def autoname(self):
        # Bypass Frappe's series-key caching (was resolving to a stale/empty
        # key and producing out-of-sequence names). Compute next number
        # directly from existing S-#### records instead.
        existing = frappe.db.sql(
            """select name from `tabMarketplace Supplier`
               where name like 'S-%%'
               order by cast(substring(name, 3) as unsigned) desc
               limit 1"""
        )
        if existing and existing[0][0]:
            try:
                next_num = int(existing[0][0].split("-")[1]) + 1
            except (IndexError, ValueError):
                next_num = 1000
        else:
            next_num = 1000
        self.name = f"S-{next_num}"

    def validate(self):
        # Suspension/blocking must cascade: no open participation
        if self.status in ("Suspended", "Blocked") and not self.is_new():
            old = self.get_doc_before_save()
            if old and old.status not in ("Suspended", "Blocked"):
                frappe.msgprint(
                    "Supplier suspended — they will no longer receive tenders.",
                    alert=True)

    def is_eligible(self):
        return bool(self.active) and self.status == "Verified"

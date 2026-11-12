import frappe
from frappe.model.document import Document


class MarketplacePartner(Document):

    def validate(self):
        if self.partner_code:
            self.partner_code = self.partner_code.strip().upper()

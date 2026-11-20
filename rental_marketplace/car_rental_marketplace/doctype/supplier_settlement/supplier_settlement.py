import frappe
from frappe.model.document import Document

STATUS_FLOW = {
    "Draft": ["Calculated"],
    "Calculated": ["Approved", "Draft"],
    "Approved": ["Processing", "Disputed"],
    "Processing": ["Paid", "Failed"],
    "Failed": ["Processing"],
    "Paid": [],
    "Disputed": ["Calculated"],
}


class SupplierSettlement(Document):

    def validate(self):
        if not self.is_new():
            old = self.get_doc_before_save()
            if old and old.status != self.status:
                allowed = STATUS_FLOW.get(old.status, [])
                if self.status not in allowed:
                    frappe.throw(
                        f"Settlement cannot move {old.status} → {self.status}.")
                if self.status in ("Approved", "Paid") and \
                        "Car Marketplace Finance" not in frappe.get_roles() and \
                        "Car Marketplace Admin" not in frappe.get_roles() and \
                        "System Manager" not in frappe.get_roles():
                    frappe.throw("Only Car Marketplace Finance may release settlements.")
        self.net_payable = round(
            (self.gross_booking_amount or 0) - (self.commission or 0)
            - (self.refunds or 0) + (self.adjustments or 0) - (self.tax or 0), 2)

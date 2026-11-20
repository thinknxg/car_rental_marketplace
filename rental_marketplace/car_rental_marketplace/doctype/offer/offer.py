import frappe
from frappe.model.document import Document

# Fields that constitute the immutable commercial snapshot after submit.
FINANCIAL_FIELDS = (
    "base_rate", "discount", "extras_amount", "delivery_fee", "one_way_fee",
    "tax_amount", "deposit_amount", "total_amount", "supplier_payout",
    "marketplace_commission", "currency", "cancellation_policy",
    "mileage_policy", "fuel_policy", "valid_until",
)

TERMINAL = ("Withdrawn", "Expired", "Accepted", "Rejected", "Superseded")


class Offer(Document):
    """Once submitted, financial terms are an immutable commercial snapshot.
    Changes go through offer_service.create_version(), which bumps
    current_version, appends an Offer Version audit record and sets
    flags.via_offer_service. Direct edits are rejected here.
    """

    def validate(self):
        if self.is_new():
            return
        old = self.get_doc_before_save()
        if not old:
            return
        if old.status in TERMINAL and not self.flags.via_offer_service:
            frappe.throw(f"Offer is {old.status} — it can no longer change.")
        if old.status != "Draft" and not self.flags.via_offer_service:
            for f in FINANCIAL_FIELDS:
                if str(self.get(f) or "") != str(old.get(f) or ""):
                    frappe.throw(
                        "Submitted offers are immutable. Use "
                        "offer_service.create_version to issue new terms.")
            if old.status != self.status and self.status not in ("Shortlisted",):
                frappe.throw(
                    "Offer status changes must go through the offer service.")

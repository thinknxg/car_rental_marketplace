import frappe
from frappe.model.document import Document


class CarPricingRule(Document):

    def validate(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            frappe.throw("Season end must be after season start.")
        if (self.minimum_days or 1) > (self.maximum_days or 90):
            frappe.throw("Min days cannot exceed max days.")

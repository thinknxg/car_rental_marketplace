import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime, cint


class CarRentalRequest(Document):

    def validate(self):
        if get_datetime(self.dropoff_datetime) <= get_datetime(self.pickup_datetime):
            frappe.throw("Dropoff must be after pickup.")
        if self.is_new() and get_datetime(self.pickup_datetime) < now_datetime():
            frappe.throw("Pickup must be in the future.")
        if self.same_return_location and not self.dropoff_location:
            self.dropoff_location = self.pickup_location
        min_age = frappe.db.get_single_value("Car Marketplace Settings", "min_driver_age") or 21
        if self.driver_age and cint(self.driver_age) < min_age:
            frappe.throw(f"Minimum driver age is {min_age}.")

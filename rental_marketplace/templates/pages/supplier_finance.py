import frappe
from rental_marketplace.templates.pages.supplier_home import _guard


def get_context(context):
    supplier = _guard()
    context.no_cache = 1
    context.settlements = frappe.get_all(
        "Supplier Settlement", filters={"supplier": supplier},
        fields=["name", "period_start", "period_end", "status",
                "gross_booking_amount", "commission", "refunds",
                "net_payable", "currency", "paid_at", "payment_reference"],
        order_by="period_start desc", limit_page_length=24)
    context.unsettled = frappe.get_all(
        "Marketplace Ledger Entry",
        filters={"supplier": supplier, "settled": 0,
                 "entry_type": ["in", ["Supplier Payable",
                                       "Supplier Adjustment"]]},
        fields=["name", "entry_type", "booking", "amount", "currency",
                "posting_datetime"],
        order_by="posting_datetime desc", limit_page_length=30)
    return context

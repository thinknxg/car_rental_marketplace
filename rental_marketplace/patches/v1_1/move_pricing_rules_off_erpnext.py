"""Give ERPNext its Pricing Rule DocType back and move this app's rules to Car Pricing Rule.

Before v1.1 this app shipped a DocType named "Pricing Rule", the same name as ERPNext's
(erpnext/accounts/doctype/pricing_rule). Installing the app replaced ERPNext's definition,
so ERPNext pricing rules could no longer be created. Runs after model sync, when
"Car Pricing Rule" exists.
"""

import frappe

NEW = "Car Pricing Rule"
# this app's rules were named format:PRL-{#####}; ERPNext's use the PRLE- series
OUR_ROWS = "name like 'PRL-%%'"


def execute():
	if not frappe.db.exists("DocType", NEW):
		return
	restore_erpnext_definition()
	if not frappe.db.table_exists("Pricing Rule"):
		return
	src_cols = set(frappe.db.get_table_columns("Pricing Rule"))
	if "daily_rate" not in src_cols:  # never held this app's rows
		return
	dst_cols = set(frappe.db.get_table_columns(NEW))
	cols = sorted(src_cols & dst_cols)
	col_sql = ", ".join(f"`{c}`" for c in cols)
	moved = frappe.db.sql(f"select count(*) from `tabPricing Rule` where {OUR_ROWS}")[0][0]
	if not moved:
		return
	frappe.db.sql(
		f"insert ignore into `tab{NEW}` ({col_sql}) select {col_sql} from `tabPricing Rule` where {OUR_ROWS}"
	)
	frappe.db.sql(f"delete from `tabPricing Rule` where {OUR_ROWS}")
	print(f"rental_marketplace: moved {moved} rule(s) from 'Pricing Rule' to '{NEW}'.")


def restore_erpnext_definition():
	if "erpnext" not in frappe.get_installed_apps():
		return
	if frappe.db.get_value("DocType", "Pricing Rule", "module") == "Accounts":
		return
	frappe.reload_doc("accounts", "doctype", "pricing_rule", force=True)
	print("rental_marketplace: restored ERPNext's Pricing Rule DocType.")

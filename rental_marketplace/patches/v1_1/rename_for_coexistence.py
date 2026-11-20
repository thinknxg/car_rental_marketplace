"""Move an existing site onto the new, collision-free names (runs before model sync).

v1.1 renamed this app's identifiers so it can share a bench and a site with
thinknxg_rental_marketplace and with ERPNext:

* module  "Rental Marketplace"  -> "Car Rental Marketplace"
* DocType "Rental Request" -> "Car Rental Request", "Marketplace Settings" ->
  "Car Marketplace Settings", "Marketplace Review" -> "Car Marketplace Review",
  "Marketplace Audit Event" -> "Car Marketplace Audit Event"
  ("Pricing Rule" -> "Car Pricing Rule" is handled after model sync, see
  move_pricing_rules_off_erpnext: that table also belongs to ERPNext)
* Roles   "Marketplace *" -> "Car Marketplace *"

Every step is guarded, so the patch is a no-op on fresh installs and safe to re-run.
Data moves with the renames (RENAME TABLE / tabSingles), nothing is dropped.
"""

import frappe
from frappe.model.rename_doc import rename_doc

APP = "rental_marketplace"
OTHER_APP = "thinknxg_rental_marketplace"
OLD_MODULE, NEW_MODULE = "Rental Marketplace", "Car Rental Marketplace"

# old name -> (new name, a field only this app's version of the DocType has)
DOCTYPES = {
	"Rental Request": ("Car Rental Request", "pickup_location"),
	"Marketplace Settings": ("Car Marketplace Settings", "w_price"),
	"Marketplace Review": ("Car Marketplace Review", "pickup_rating"),
	"Marketplace Audit Event": ("Car Marketplace Audit Event", "entity_type"),
}

# DocTypes whose names did not change; they only move module
UNCHANGED_DOCTYPES = [
	"API Idempotency Key", "Booking", "Booking Message", "Cancellation Policy", "Customer Profile",
	"Marketplace Extra", "Marketplace Ledger Entry", "Marketplace Location", "Marketplace Partner",
	"Marketplace Refund", "Marketplace Supplier", "Marketplace Vehicle", "Offer", "Offer Extra",
	"Offer Version", "Payment Intent", "Payment Transaction", "Payment Webhook Event",
	"Rental Request Extra", "Supplier Portal User", "Supplier Service Area", "Supplier Settlement",
	"Supplier Settlement Line", "Tender", "Tender Supplier", "Vehicle Availability Block", "Vehicle Class",
]

ROLES = {
	"Marketplace Admin": "Car Marketplace Admin",
	"Marketplace Operations": "Car Marketplace Operations",
	"Marketplace Finance": "Car Marketplace Finance",
	"Marketplace Support": "Car Marketplace Support",
	"Marketplace Customer": "Car Marketplace Customer",
}
# role names thinknxg_rental_marketplace also uses
SHARED_ROLES = {"Marketplace Admin", "Marketplace Finance", "Marketplace Customer"}


def execute():
	other_installed = OTHER_APP in frappe.get_installed_apps()
	move_module(other_installed)
	rename_doctypes(other_installed)
	rename_roles(other_installed)
	frappe.clear_cache()


def _is_ours(doctype, signature_field):
	return bool(frappe.db.exists("DocField", {"parent": doctype, "fieldname": signature_field}))


def move_module(other_installed):
	if not frappe.db.exists("Module Def", OLD_MODULE):
		return
	if not frappe.db.exists("Module Def", NEW_MODULE):
		frappe.get_doc({"doctype": "Module Def", "module_name": NEW_MODULE, "app_name": APP}).insert(
			ignore_permissions=True
		)

	ours = list(UNCHANGED_DOCTYPES) + ["Pricing Rule"]
	ours += [old for old, (_new, sig) in DOCTYPES.items() if _is_ours(old, sig)]
	for dt in ours:
		if frappe.db.get_value("DocType", dt, "module") == OLD_MODULE:
			frappe.db.set_value("DocType", dt, "module", NEW_MODULE, update_modified=False)

	# This app never shipped reports, pages, workspaces or web forms, but a site may have
	# added some to the module. They belong to whichever app keeps the old module name.
	if frappe.db.count("DocType", {"module": OLD_MODULE}):
		# thinknxg_rental_marketplace also ships a module called "Rental Marketplace":
		# hand the row to it so uninstalling this app can never delete its DocTypes.
		if other_installed:
			frappe.db.set_value("Module Def", OLD_MODULE, "app_name", OTHER_APP, update_modified=False)
		return

	if other_installed:
		frappe.db.set_value("Module Def", OLD_MODULE, "app_name", OTHER_APP, update_modified=False)
	elif frappe.db.get_value("Module Def", OLD_MODULE, "app_name") in (APP, None, ""):
		_relink_module_rows()
		frappe.delete_doc("Module Def", OLD_MODULE, ignore_permissions=True, force=True)


def _relink_module_rows():
	"""Point any remaining Link-to-Module-Def values (reports, workspaces, ...) at the new module."""
	for df in frappe.get_all(
		"DocField", filters={"fieldtype": "Link", "options": "Module Def"}, fields=["parent", "fieldname"]
	):
		if df.parent == "DocType" or not frappe.db.exists("DocType", df.parent):
			continue
		if not frappe.db.table_exists(df.parent) or not frappe.db.has_column(df.parent, df.fieldname):
			continue
		frappe.db.sql(
			f"update `tab{df.parent}` set `{df.fieldname}`=%s where `{df.fieldname}`=%s",
			(NEW_MODULE, OLD_MODULE),
		)


def rename_doctypes(other_installed):
	for old, (new, sig) in DOCTYPES.items():
		if not frappe.db.exists("DocType", old) or frappe.db.exists("DocType", new):
			continue
		if not _is_ours(old, sig):
			continue  # thinknxg_rental_marketplace's DocType of the same name: leave it alone
		if other_installed:
			# Both apps wrote into one shared table before v1.1; its rows cannot be split
			# automatically. The new DocType is created empty by model sync.
			print(f"rental_marketplace: '{old}' is shared with {OTHER_APP}; not moving its rows to '{new}'.")
			continue
		rename_doc("DocType", old, new, force=True, ignore_permissions=True, show_alert=False)
		_update_doctype_name_values(old, new)


def _update_doctype_name_values(old, new):
	"""Data fields that store a DocType name are not touched by rename_doc."""
	for dt, field in (("Car Marketplace Audit Event", "entity_type"), ("Marketplace Audit Event", "entity_type"),
			("Marketplace Ledger Entry", "reference_doctype")):
		if frappe.db.table_exists(dt) and frappe.db.has_column(dt, field):
			frappe.db.sql(f"update `tab{dt}` set `{field}`=%s where `{field}`=%s", (new, old))


def rename_roles(other_installed):
	for old, new in ROLES.items():
		if not frappe.db.exists("Role", old):
			continue
		if not frappe.db.exists("Role", new):
			if other_installed and old in SHARED_ROLES:
				_copy_role(old, new)
				continue
			rename_doc("Role", old, new, force=True, ignore_permissions=True, show_alert=False)
		elif not (other_installed and old in SHARED_ROLES):
			_copy_role(old, new)


def _copy_role(old, new):
	"""Shared role name: keep the old role for the other app, give its holders the new one too.

	Review the holders afterwards: users who only work in one of the two apps should keep
	only that app's role."""
	if not frappe.db.exists("Role", new):
		desk = frappe.db.get_value("Role", old, "desk_access")
		frappe.get_doc({"doctype": "Role", "role_name": new, "desk_access": desk}).insert(ignore_permissions=True)
	users = frappe.get_all("Has Role", filters={"role": old, "parenttype": "User"}, pluck="parent")
	for user in set(users):
		if not frappe.db.exists("Has Role", {"parent": user, "parenttype": "User", "role": new}):
			frappe.get_doc("User", user).add_roles(new)
	if users:
		print(f"rental_marketplace: gave '{new}' to {len(set(users))} holder(s) of shared role '{old}'; review them.")

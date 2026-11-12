import frappe


def get_context(context):
    context.no_cache = 1
    context.full_width = True
    context.success = False
    context.message = ""
    context.booking = None

    intent_name = frappe.form_dict.get("intent")
    cancelled = frappe.form_dict.get("cancelled")
    token = frappe.form_dict.get("token")  # PayPal's order id, same as our provider_intent_id

    if cancelled:
        context.message = "Payment was cancelled."
        return context

    if not intent_name or not token:
        context.message = "Invalid payment return — missing reference."
        return context

    from rental_marketplace.services import payment_service
    try:
        result = payment_service.complete_paypal_payment(token)
        context.success = True
        context.booking = result.get("booking")
        context.message = "Payment received — your booking is confirmed."
    except Exception:
        frappe.log_error(frappe.get_traceback(), "PayPal return handling failed")
        context.message = "We couldn't confirm your payment. If you were charged, contact support and we'll sort it out."

    return context
